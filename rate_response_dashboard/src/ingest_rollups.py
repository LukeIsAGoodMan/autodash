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


def plan_ingestion(cfg: dict, force: bool = False) -> list[IngestPlan]:
    """Build the plan without touching the mart.

    Prints (at INFO) the directory being scanned, whether it exists, and
    every file in it. This is the diagnostic surface for "0 files" issues.

    If `force` is True, every parseable CSV becomes a replace action,
    bypassing the mtime / refresh-window heuristics. Use for re-ingesting
    after a schema change in ingest_rollups.
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

        if force and part_mtime is not None:
            action, reason = "replace", "forced re-ingest (--force)"
        elif part_mtime is None:
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

    SAS `proc export` writes missing numeric values as a literal '.'. Without
    telling polars about it, a column that is all-'.' (which happens whenever
    EXP_RESPONSE_SCORE is missing for a whole month) gets inferred as String,
    and downstream .sum() raises 'sum operation not supported for dtype str'.
    Passing null_values=['', '.'] makes polars correctly parse those as null.
    """
    df = pl.read_csv(
        str(path),
        infer_schema_length=10_000,
        null_values=["", "."],
    )
    log.info("loaded %s: %d rows, columns=%s", path.name, df.height, df.columns)
    return df


def _canonicalize_columns(df: pl.DataFrame, canonical_names: list[str]) -> pl.DataFrame:
    """Case-insensitively rename CSV columns to canonical names from config.

    SAS proc export emits column names in whatever case the underlying SAS
    dataset stored — frequently UPPERCASE for variables sourced from
    datalake.experianprescreen and lowercase for variables created in a SQL
    SELECT. Our config uses one fixed canonical case per column; this
    function bridges the gap so validation does not reject files that have
    the data but the wrong case.
    """
    lower_to_actual = {c.lower(): c for c in df.columns}
    rename_map: dict[str, str] = {}
    for canonical in canonical_names:
        key = canonical.lower()
        actual = lower_to_actual.get(key)
        if actual is None or actual == canonical:
            continue
        rename_map[actual] = canonical
    if rename_map:
        log.info("canonicalizing column case: %s", rename_map)
        df = df.rename(rename_map)
    return df


def _prepare_for_mart(df: pl.DataFrame, cm: CampaignMonth, cfg: dict) -> pl.DataFrame:
    """Normalize the frame before writing.

    Five transforms only:
      0. canonicalize column case (handle SAS proc export quirks)
      1. drop SAS precomputed rate columns (force dashboard to recompute)
      2. backfill optional columns the CSV is missing (e.g. expected_responses_xpm)
      3. attach campaign_month as a string column
      4. coerce flag columns to Int32 if they exist
    """
    # 0. case canonicalization: collect every known canonical name from config
    #    and rename any case-variant present in the CSV.
    canonical: set[str] = set()
    canonical.update(cfg["mart"]["required_columns"])
    canonical.update((cfg["mart"].get("optional_columns") or {}).keys())
    canonical.update(cfg["mart"].get("drop_precomputed_rate_columns") or [])
    canonical.update(cfg.get("catalog", {}).get("dimensions") or [])
    df = _canonicalize_columns(df, sorted(canonical))

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

    # vs_band normalization: SAS produces blanks when vantage3 is null or
    # below 530. Per analyst direction, treat all such rows as the lowest
    # band '530-549' so they don't disappear from filters and pivots.
    if "vs_band" in df.columns:
        df = df.with_columns(
            pl.when(
                pl.col("vs_band").is_null()
                | (pl.col("vs_band").cast(pl.Utf8).str.strip_chars() == "")
            )
            .then(pl.lit("530-549"))
            .otherwise(pl.col("vs_band"))
            .alias("vs_band")
        )

    df = df.with_columns(pl.lit(cm.iso).alias("campaign_month"))

    flag_cols = [
        "pqabandon_flag", "prchargeoff_flag", "prclosure_flag",
        "prdecline_flag", "rm_flag", "scorecard", "trm10_tier",
    ]
    for c in flag_cols:
        if c in df.columns:
            df = df.with_columns(pl.col(c).cast(pl.Int32, strict=False))

    # Numeric metric columns: belt-and-suspenders cast to Float64. If a column
    # was inferred as String (because all-null markers slipped past the
    # null_values config, or because SAS emitted something unusual), this
    # coerces unparseable cells to null instead of crashing validation.
    numeric_metric_cols = [
        "volume", "responders", "Boards",
        "expected_responses", "expected_responses_xpm",
    ]
    for c in numeric_metric_cols:
        if c in df.columns and df[c].dtype != pl.Float64:
            df = df.with_columns(pl.col(c).cast(pl.Float64, strict=False))
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
            # Duck-typed empty ValidationResult — make_load_log_row only reads
            # `.stats` off it. The kwarg name is `result` (not `vr`); using
            # `vr=` was a bug that turned a real failure into a TypeError and
            # short-circuited the rest of the ingest loop.
            stub = type("V", (), {"stats": {}})()
            row = make_load_log_row(
                p.cm, p.csv_path, str(p.csv_mtime),
                result=stub,
                status="failed", message=f"exception: {e!r}",
            )
            append_load_log(log_path, row)

    rebuild_validation_summary(
        mart_dir,
        Path(cfg["paths"]["logs_dir"]) / "validation_summary.csv",
        logs_dir=cfg["paths"]["logs_dir"],
        maturity_threshold_months=cfg["mart"].get("maturity_threshold_months", 3),
    )
    log.info("ingest summary: %s", summary)
    return summary


def run_ingest(cfg: dict, expected_months: list[str] | None = None,
               force: bool = False) -> dict:
    """One-call entrypoint used by scripts.

    If `expected_months` is provided (list of 'YYYY-MM'), we additionally check
    that each is present in the plan with action add/replace. Months that
    appear nowhere in the plan are reported as 'missing_csv' so the caller
    can fail fast.
    """
    plan = plan_ingestion(cfg, force=force)
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
