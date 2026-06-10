"""Decile-grain ingest. Mirrors src.ingest_rollups but for the smaller
`exp_<MMMYY>_decile.csv` files that SAS %rollup_decile emits.

Mart layout:
    data/mart/decile_rollup/
        campaign_month=2025-01/decile.parquet
        campaign_month=2025-02/decile.parquet
        ...

Each parquet has columns:
    campaign_month  scorecard  total_decile  volume  responders  Boards

Best-effort by design: if there are no decile CSVs (e.g. mart was built
before SAS started emitting them), this returns a zero-action summary
without raising. Old months can be backfilled later by re-running
`scripts/run_monthly_refresh.py --month YYYY-MM` once the new SAS macro is
installed on the server.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import polars as pl

from .ingest_rollups import _canonicalize_columns, _load_csv
from .utils import CampaignMonth, ensure_dir, parse_decile_filename

log = logging.getLogger(__name__)

PARTITION_PREFIX = "campaign_month="
DECILE_FILENAME = "decile.parquet"

Action = Literal["add", "replace", "skip"]


@dataclass
class DecilePlan:
    cm: CampaignMonth
    csv_path: Path
    csv_mtime: datetime
    partition_mtime: datetime | None
    action: Action
    reason: str


# ------------------------------------------------------------- planning
def _partition_dir(mart_dir: Path, cm: CampaignMonth) -> Path:
    return mart_dir / f"{PARTITION_PREFIX}{cm.iso}"


def _partition_mtime(mart_dir: Path, cm: CampaignMonth) -> datetime | None:
    p = _partition_dir(mart_dir, cm) / DECILE_FILENAME
    if not p.exists():
        return None
    return datetime.fromtimestamp(p.stat().st_mtime)


def plan_decile_ingestion(cfg: dict, force: bool = False) -> list[DecilePlan]:
    """Scan rollup_csv_dir for exp_*_decile.csv and decide per-month action."""
    csv_dir = Path(cfg["paths"]["rollup_csv_dir"])
    mart_dir = Path(cfg["paths"]["decile_mart_dir"])

    log.info("Scanning for decile CSVs in %s", csv_dir)
    if not csv_dir.exists():
        log.warning("rollup_csv_dir does not exist: %s", csv_dir)
        return []

    plans: list[DecilePlan] = []
    for path in sorted(csv_dir.glob("exp_*_decile.csv")):
        cm = parse_decile_filename(path.name)
        if cm is None:
            continue
        csv_mtime = datetime.fromtimestamp(path.stat().st_mtime)
        part_mtime = _partition_mtime(mart_dir, cm)

        if force and part_mtime is not None:
            action, reason = "replace", "forced re-ingest (--force)"
        elif part_mtime is None:
            action, reason = "add", "new month — no existing partition"
        elif csv_mtime > part_mtime:
            action, reason = "replace", f"csv newer than partition ({csv_mtime} > {part_mtime})"
        else:
            action, reason = "skip", "csv not newer than partition"
        plans.append(DecilePlan(cm, path, csv_mtime, part_mtime, action, reason))

    return plans


# ------------------------------------------------------------- execution
def _prepare(df: pl.DataFrame, cm: CampaignMonth, cfg: dict) -> pl.DataFrame:
    """Canonicalize column case, coerce integer dims, attach campaign_month."""
    canonical = list(cfg["mart"].get("decile_required_columns") or [])
    df = _canonicalize_columns(df, canonical)
    for c in ("scorecard", "total_decile"):
        if c in df.columns:
            df = df.with_columns(pl.col(c).cast(pl.Int32, strict=False))
    df = df.with_columns(pl.lit(cm.iso).alias("campaign_month"))
    return df


def _validate(df: pl.DataFrame, required: list[str]) -> tuple[bool, str]:
    missing = [c for c in required if c not in df.columns]
    if missing:
        return False, f"missing columns {missing}"
    if df.is_empty():
        return False, "empty file"
    vmin = df.select(pl.col("volume").min()).item()
    if vmin is not None and vmin < 0:
        return False, f"negative volume (min={vmin})"
    return True, ""


def _safe_write(df: pl.DataFrame, mart_dir: Path, cm: CampaignMonth) -> Path:
    """tmp dir → atomic swap. Same pattern as build_mart.write_partition_safely."""
    final = _partition_dir(mart_dir, cm)
    tmp = mart_dir / f"{PARTITION_PREFIX}{cm.iso}__tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    ensure_dir(tmp)
    out = tmp / DECILE_FILENAME
    df.write_parquet(str(out), compression="snappy")
    log.info("wrote %s (%d rows)", out, df.height)
    if final.exists():
        shutil.rmtree(final)
    tmp.rename(final)
    return final


def execute_decile_plan(plan: list[DecilePlan], cfg: dict) -> dict:
    """Run the plan. Returns a small summary dict for the caller to log."""
    mart_dir = ensure_dir(cfg["paths"]["decile_mart_dir"])
    required = cfg["mart"].get("decile_required_columns") or []
    summary = {"added": 0, "replaced": 0, "skipped": 0, "failed": 0}

    for p in plan:
        if p.action == "skip":
            summary["skipped"] += 1
            log.info("[skip-decile] %s: %s", p.cm.iso, p.reason)
            continue
        try:
            raw = _load_csv(p.csv_path)
            df = _prepare(raw, p.cm, cfg)
            ok, why = _validate(df, required)
            if not ok:
                summary["failed"] += 1
                log.error("[fail-decile] %s: %s", p.cm.iso, why)
                continue
            _safe_write(df, mart_dir, p.cm)
            status = "added" if p.action == "add" else "replaced"
            summary[status] += 1
            log.info("[%s-decile] %s: %s", status, p.cm.iso, p.reason)
        except Exception as e:  # noqa: BLE001
            summary["failed"] += 1
            log.exception("[fail-decile] %s: unexpected error: %s", p.cm.iso, e)
    return summary


def run_decile_ingest(cfg: dict, force: bool = False) -> dict:
    """One-call entrypoint. Returns even when no decile CSVs are present."""
    plan = plan_decile_ingestion(cfg, force=force)
    if not plan:
        log.info("No decile CSVs found — skipping decile ingest (P4 step).")
        return {"added": 0, "replaced": 0, "skipped": 0, "failed": 0}
    log.info("decile ingest plan: %d files (%s)", len(plan),
             ", ".join(f"{p.cm.iso}:{p.action}" for p in plan))
    summary = execute_decile_plan(plan, cfg)
    log.info("decile ingest summary: %s", summary)
    return summary


# ------------------------------------------------------------- mart read
_NUMERIC_DECILE_COLS = ["volume", "responders", "Boards"]


def read_decile_mart(decile_mart_dir: str | Path) -> pl.DataFrame:
    """Scan decile partitions, harmonizing numeric dtypes across files.

    Same approach as build_mart.read_mart — eager per-partition read + cast
    to Float64 before concat — so partitions written under different schema
    versions union without SchemaError.
    """
    p = Path(decile_mart_dir)
    pq_files = sorted(p.glob(f"{PARTITION_PREFIX}*/{DECILE_FILENAME}"))
    if not pq_files:
        return pl.DataFrame()

    frames: list[pl.DataFrame] = []
    for f in pq_files:
        try:
            df = pl.read_parquet(str(f))
        except Exception:
            continue
        if "campaign_month" not in df.columns:
            cm = f.parent.name.split("=", 1)[1]
            df = df.with_columns(pl.lit(cm).alias("campaign_month"))
        for c in _NUMERIC_DECILE_COLS:
            if c in df.columns and df[c].dtype != pl.Float64:
                df = df.with_columns(pl.col(c).cast(pl.Float64, strict=False))
        frames.append(df)

    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")
