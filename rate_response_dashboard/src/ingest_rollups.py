"""Scan SAS exports → diff vs mart → load new/updated months.

Decision tree for every exp_<MMMYY>_rollup.csv found:

    csv mtime vs partition mtime    in refresh window?   action
    --------------------------------------------------------------
    partition missing               n/a                  ADD
    csv newer than partition        n/a                  REPLACE
    csv same or older               yes (recent months)  REPLACE (forced)
    csv same or older               no                   SKIP

This is the only place a partition is created or overwritten. We never
.append; every load is a full-month rewrite. That is what kills the
double-counting risk.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import polars as pl

from .build_mart import (
    discard_tmp_partition,
    partition_dir,
    write_partition_safely,
)
from .utils import CampaignMonth, ensure_dir, now_iso, parse_rollup_filename
from .validation import (
    append_load_log,
    make_load_log_row,
    rebuild_validation_summary,
    validate_rollup_frame,
)

log = logging.getLogger(__name__)

Action = Literal["add", "replace", "skip"]


@dataclass
class IngestPlan:
    cm: CampaignMonth
    csv_path: Path
    csv_mtime: datetime
    partition_mtime: datetime | None
    action: Action
    reason: str


# ----------------------------------------------------------- planning
def _partition_mtime(mart_dir: Path, cm: CampaignMonth) -> datetime | None:
    p = partition_dir(mart_dir, cm) / "rollup.parquet"
    if not p.exists():
        return None
    return datetime.fromtimestamp(p.stat().st_mtime)


def _is_recent(cm: CampaignMonth, refresh_window_months: int) -> bool:
    """Force refresh for the last N months relative to today."""
    today = datetime.today()
    months_ago = (today.year - cm.year) * 12 + (today.month - cm.month)
    return 0 <= months_ago < refresh_window_months


def plan_ingestion(cfg: dict) -> list[IngestPlan]:
    """Build the plan without touching the mart.

    Prints (at INFO) the directory being scanned, whether it exists, and
    every file in it. This is the diagnostic surface for "0 files" issues.
    """
    csv_dir = Path(cfg["paths"]["rollup_csv_dir"])
    mart_dir = Path(cfg["paths"]["mart_dir"])
    refresh_window = cfg["refresh"]["recent_refresh_window_months"]
    freeze_months = cfg["refresh"]["history_freeze_months"]

    log.info("Scanning rollup_csv_dir = %s", csv_dir)
    log.info("Directory exists       = %s", csv_dir.exists())
    if csv_dir.exists():
        all_files = sorted(p.name for p in csv_dir.iterdir() if p.is_file())
        log.info("Files present (%d total): %s",
                 len(all_files),
                 all_files if len(all_files) <= 50 else all_files[:50] + ["..."])
    else:
        log.error(
            "rollup_csv_dir does not exist: %s. "
            "Check config.paths.rollup_csv_dir matches the SAS &folder. macro "
            "(config.sas.export_folder_macrovar = %s).",
            csv_dir, cfg["sas"]["export_folder_macrovar"],
        )
        return []

    today = datetime.today()
    plans: list[IngestPlan] = []
    for path in sorted(csv_dir.glob("exp_*_rollup.csv")):
        cm = parse_rollup_filename(path.name)
        if cm is None:
            log.warning("skipping unparseable file: %s", path.name)
            continue

        # freeze: refuse to touch anything older than freeze_months
        months_old = (today.year - cm.year) * 12 + (today.month - cm.month)
        if months_old >= freeze_months:
            log.debug("freeze window: skip %s (months_old=%d)", path.name, months_old)
            continue

        csv_mtime = datetime.fromtimestamp(path.stat().st_mtime)
        part_mtime = _partition_mtime(mart_dir, cm)

        if part_mtime is None:
            action, reason = "add", "new month — no existing partition"
        elif csv_mtime > part_mtime:
            action, reason = "replace", f"csv newer than partition ({csv_mtime} > {part_mtime})"
        elif _is_recent(cm, refresh_window):
            action, reason = "replace", f"within {refresh_window}-month refresh window"
        else:
            action, reason = "skip", "csv not newer and outside refresh window"

        plans.append(IngestPlan(cm, path, csv_mtime, part_mtime, action, reason))

    return plans


# ----------------------------------------------------------- execution
def _load_csv(path: Path) -> pl.DataFrame:
    """Read a SAS-exported rollup CSV with permissive typing.

    SAS's dbms=csv export tends to emit numbers as numerics already, but
    integer flags may come through as floats. We accept whatever comes and
    rely on the validation step to bounce malformed files.
    """
    return pl.read_csv(str(path), infer_schema_length=10_000)


def _prepare_for_mart(df: pl.DataFrame, cm: CampaignMonth, cfg: dict) -> pl.DataFrame:
    """Normalize the frame before writing.

    Four transforms only:
      1. drop SAS precomputed rate columns (force dashboard to recompute)
      2. backfill optional columns the CSV is missing (e.g. expected_responses_xpm)
      3. attach campaign_month as a string column
      4. coerce flag columns to Int32 if they exist
    """
    drop_cols = [c for c in cfg["mart"]["drop_precomputed_rate_columns"] if c in df.columns]
    if drop_cols:
        df = df.drop(drop_cols)

    # Backfill optional columns. If the CSV doesn't have it, add it with the
    # configured default (typically null) so downstream code can safely
    # reference the column. The polars dtype defaults to Float64 for null
    # literals; cast explicitly so sums don't surprise us.
    optional_cols = cfg["mart"].get("optional_columns") or {}
    for col, default in optional_cols.items():
        if col not in df.columns:
            df = df.with_columns(
                pl.lit(default, dtype=pl.Float64).alias(col)
            )
            log.info("partition %s: optional column %r missing in CSV, "
                     "filled with default=%r", cm.iso, col, default)

    df = df.with_columns(pl.lit(cm.iso).alias("campaign_month"))

    flag_cols = [
        "pqabandon_flag", "prchargeoff_flag", "prclosure_flag",
        "prdecline_flag", "rm_flag", "scorecard", "trm10_tier",
    ]
    for c in flag_cols:
        if c in df.columns:
            df = df.with_columns(pl.col(c).cast(pl.Int32, strict=False))
    return df


def execute_plan(plan: list[IngestPlan], cfg: dict) -> dict:
    """Run the plan. Returns a small summary dict for the caller to print."""
    mart_dir = ensure_dir(cfg["paths"]["mart_dir"])
    log_path = Path(cfg["paths"]["logs_dir"]) / "load_log.csv"
    ensure_dir(log_path.parent)

    required = cfg["mart"]["required_columns"]
    summary = {"added": 0, "replaced": 0, "skipped": 0, "failed": 0}

    for p in plan:
        if p.action == "skip":
            summary["skipped"] += 1
            log.info("[skip] %s: %s", p.cm.iso, p.reason)
            continue

        try:
            raw = _load_csv(p.csv_path)
            df = _prepare_for_mart(raw, p.cm, cfg)
            vr = validate_rollup_frame(df, required, p.cm)

            if not vr.ok:
                summary["failed"] += 1
                discard_tmp_partition(mart_dir, p.cm)
                row = make_load_log_row(
                    p.cm, p.csv_path, str(p.csv_mtime),
                    vr, status="failed", message="; ".join(vr.errors),
                )
                append_load_log(log_path, row)
                log.error("[fail] %s: %s", p.cm.iso, vr.errors)
                continue

            write_partition_safely(df, mart_dir, p.cm)
            status = "added" if p.action == "add" else "replaced"
            summary[status] += 1

            row = make_load_log_row(
                p.cm, p.csv_path, str(p.csv_mtime),
                vr, status=status,
                message="; ".join(vr.warnings) if vr.warnings else "",
            )
            append_load_log(log_path, row)
            log.info("[%s] %s: %s", status, p.cm.iso, p.reason)

        except Exception as e:  # noqa: BLE001 — boundary: surface but continue
            summary["failed"] += 1
            discard_tmp_partition(mart_dir, p.cm)
            log.exception("[fail] %s: unexpected error", p.cm.iso)
            row = make_load_log_row(
                p.cm, p.csv_path, str(p.csv_mtime),
                vr=type("V", (), {"stats": {}})(),  # empty stats
                status="failed", message=f"exception: {e!r}",
            )
            append_load_log(log_path, row)

    rebuild_validation_summary(
        mart_dir, Path(cfg["paths"]["logs_dir"]) / "validation_summary.csv"
    )
    log.info("ingest summary: %s", summary)
    return summary


def run_ingest(cfg: dict, expected_months: list[str] | None = None) -> dict:
    """One-call entrypoint used by scripts.

    If `expected_months` is provided (list of 'YYYY-MM'), we additionally check
    that each is present in the plan with action add/replace. Months that
    appear nowhere in the plan are reported as 'missing_csv' so the caller
    can fail fast.
    """
    plan = plan_ingestion(cfg)
    log.info("ingest plan: %d files (%s)", len(plan),
             ", ".join(f"{p.cm.iso}:{p.action}" for p in plan))

    if not plan:
        log.warning(
            "No rollup CSV files found in %s. "
            "Check SAS export path and SAS log under %s/sas_*.log.",
            cfg["paths"]["rollup_csv_dir"], cfg["paths"]["logs_dir"],
        )

    summary = execute_plan(plan, cfg)

    if expected_months:
        found_months = {p.cm.iso for p in plan}
        missing = [m for m in expected_months if m not in found_months]
        summary["missing_csv"] = missing
        if missing:
            log.error(
                "Expected rollup CSVs not found for months: %s. "
                "Confirm SAS proc export wrote exp_<MMMYY>_rollup.csv to %s.",
                missing, cfg["paths"]["rollup_csv_dir"],
            )
    return summary
