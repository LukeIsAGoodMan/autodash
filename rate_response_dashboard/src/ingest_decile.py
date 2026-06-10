"""Decile-grain ingest. Handles two SAS output streams:

  - exp_<MMMYY>_decile_sc.csv   → data/mart/decile_sc/    (scorecard × 10 deciles)
  - exp_<MMMYY>_decile_port.csv → data/mart/decile_port/  (20 deciles, no scorecard split)

Each parquet has columns:
    campaign_month  [scorecard]  decile  volume  responders  Boards

A small `_DECILE_KINDS` table at module top declares both streams; the rest
of the module is parameterized so adding more decile variants later is a
one-line config change.

Best-effort by design: if SAS hasn't emitted a given file kind yet, the
loop quietly reports zero actions for it and proceeds.
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

import polars as pl

from .ingest_rollups import _canonicalize_columns, _load_csv
from .utils import (
    CampaignMonth,
    ensure_dir,
    parse_decile_port_filename,
    parse_decile_sc_filename,
)

log = logging.getLogger(__name__)

PARTITION_PREFIX = "campaign_month="
PARQUET_FILENAME = "decile.parquet"

Action = Literal["add", "replace", "skip"]


# --- declare both decile streams in one place -----------------------------
@dataclass(frozen=True)
class DecileKind:
    name: str                            # short label for logs
    csv_glob: str                        # pattern under rollup_csv_dir
    parse: Callable[[str], CampaignMonth | None]
    mart_path_key: str                   # key under cfg["paths"]
    required_cols_key: str               # key under cfg["mart"]


_DECILE_KINDS: list[DecileKind] = [
    DecileKind(
        name="decile_sc",
        csv_glob="exp_*_decile_sc.csv",
        parse=parse_decile_sc_filename,
        mart_path_key="decile_sc_mart_dir",
        required_cols_key="decile_sc_required_columns",
    ),
    DecileKind(
        name="decile_port",
        csv_glob="exp_*_decile_port.csv",
        parse=parse_decile_port_filename,
        mart_path_key="decile_port_mart_dir",
        required_cols_key="decile_port_required_columns",
    ),
]


@dataclass
class DecilePlan:
    cm: CampaignMonth
    csv_path: Path
    csv_mtime: datetime
    partition_mtime: datetime | None
    action: Action
    reason: str


# ---------------------------------------------------------- planning helpers
def _partition_dir(mart_dir: Path, cm: CampaignMonth) -> Path:
    return mart_dir / f"{PARTITION_PREFIX}{cm.iso}"


def _partition_mtime(mart_dir: Path, cm: CampaignMonth) -> datetime | None:
    p = _partition_dir(mart_dir, cm) / PARQUET_FILENAME
    if not p.exists():
        return None
    return datetime.fromtimestamp(p.stat().st_mtime)


def _plan_one_kind(cfg: dict, kind: DecileKind, force: bool) -> list[DecilePlan]:
    csv_dir = Path(cfg["paths"]["rollup_csv_dir"])
    mart_dir = Path(cfg["paths"][kind.mart_path_key])
    if not csv_dir.exists():
        return []
    plans: list[DecilePlan] = []
    for path in sorted(csv_dir.glob(kind.csv_glob)):
        cm = kind.parse(path.name)
        if cm is None:
            continue
        csv_mtime = datetime.fromtimestamp(path.stat().st_mtime)
        part_mtime = _partition_mtime(mart_dir, cm)
        if force and part_mtime is not None:
            action, reason = "replace", "forced (--force)"
        elif part_mtime is None:
            action, reason = "add", "new month"
        elif csv_mtime > part_mtime:
            action, reason = "replace", f"csv newer ({csv_mtime} > {part_mtime})"
        else:
            action, reason = "skip", "csv not newer"
        plans.append(DecilePlan(cm, path, csv_mtime, part_mtime, action, reason))
    return plans


# ---------------------------------------------------------- per-kind execution
def _prepare(df: pl.DataFrame, cm: CampaignMonth, required: list[str]) -> pl.DataFrame:
    df = _canonicalize_columns(df, required)
    for c in ("scorecard", "decile"):
        if c in df.columns:
            df = df.with_columns(pl.col(c).cast(pl.Int32, strict=False))
    for c in ("volume", "responders", "Boards"):
        if c in df.columns and df[c].dtype != pl.Float64:
            df = df.with_columns(pl.col(c).cast(pl.Float64, strict=False))
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
    final = _partition_dir(mart_dir, cm)
    tmp = mart_dir / f"{PARTITION_PREFIX}{cm.iso}__tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    ensure_dir(tmp)
    out = tmp / PARQUET_FILENAME
    df.write_parquet(str(out), compression="snappy")
    log.info("wrote %s (%d rows)", out, df.height)
    if final.exists():
        shutil.rmtree(final)
    tmp.rename(final)
    return final


def _execute_one_kind(plan: list[DecilePlan], cfg: dict, kind: DecileKind) -> dict:
    mart_dir = ensure_dir(cfg["paths"][kind.mart_path_key])
    required = cfg["mart"].get(kind.required_cols_key) or []
    summary = {"added": 0, "replaced": 0, "skipped": 0, "failed": 0}

    for p in plan:
        if p.action == "skip":
            summary["skipped"] += 1
            log.info("[skip-%s] %s: %s", kind.name, p.cm.iso, p.reason)
            continue
        try:
            raw = _load_csv(p.csv_path)
            df = _prepare(raw, p.cm, required)
            ok, why = _validate(df, required)
            if not ok:
                summary["failed"] += 1
                log.error("[fail-%s] %s: %s", kind.name, p.cm.iso, why)
                continue
            _safe_write(df, mart_dir, p.cm)
            status = "added" if p.action == "add" else "replaced"
            summary[status] += 1
            log.info("[%s-%s] %s: %s", status, kind.name, p.cm.iso, p.reason)
        except Exception as e:  # noqa: BLE001
            summary["failed"] += 1
            log.exception("[fail-%s] %s: %s", kind.name, p.cm.iso, e)
    return summary


# ---------------------------------------------------------- public API
def run_decile_ingest(cfg: dict, force: bool = False) -> dict:
    """Ingest both decile streams. Returns combined summary per kind."""
    combined = {}
    for kind in _DECILE_KINDS:
        plan = _plan_one_kind(cfg, kind, force=force)
        if not plan:
            log.info("[%s] no CSVs found — skipping", kind.name)
            combined[kind.name] = {"added": 0, "replaced": 0, "skipped": 0, "failed": 0}
            continue
        log.info("[%s] plan: %d files (%s)", kind.name, len(plan),
                 ", ".join(f"{p.cm.iso}:{p.action}" for p in plan))
        combined[kind.name] = _execute_one_kind(plan, cfg, kind)
    return combined


# ---------------------------------------------------------- mart reads
_NUMERIC_DECILE_COLS = ["volume", "responders", "Boards"]


def _read_one_decile_mart(mart_dir: str | Path) -> pl.DataFrame:
    p = Path(mart_dir)
    pq_files = sorted(p.glob(f"{PARTITION_PREFIX}*/{PARQUET_FILENAME}"))
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


def read_decile_sc_mart(decile_sc_mart_dir: str | Path) -> pl.DataFrame:
    """Scorecard-level decile mart (10 deciles per scorecard, per month)."""
    return _read_one_decile_mart(decile_sc_mart_dir)


def read_decile_port_mart(decile_port_mart_dir: str | Path) -> pl.DataFrame:
    """Portfolio-level decile mart (20 deciles across all customers, per month)."""
    return _read_one_decile_mart(decile_port_mart_dir)
