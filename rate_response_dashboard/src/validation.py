"""Validation + load log.

Two outputs land on disk:
  data/logs/load_log.csv         - one row per (campaign_month, load_attempt)
  data/logs/validation_summary.csv - one row per campaign_month currently in mart

These two files are also what the Dashboard's Data Quality tab reads.

Validation rules (intentionally conservative — failing any rule blocks the
partition write so the mart never gets polluted):
  1. required columns present
  2. row count > 0
  3. volume column is non-negative, non-empty
  4. sum(responders) <= sum(volume)
  5. sum(Boards) <= sum(volume)
  6. sum(expected_responses) is in [0, sum(volume)]
  7. campaign_month value (when present on the frame) is unique
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

import polars as pl

from .utils import CampaignMonth, ensure_dir, now_iso

log = logging.getLogger(__name__)

LOAD_LOG_COLUMNS = [
    "campaign_month",
    "source_file",
    "source_modified_time",
    "loaded_time",
    "row_count",
    "total_volume",
    "total_responders",
    "total_boards",
    "total_expected_responses",
    "total_expected_responses_xpm",
    "status",
    "message",
]


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def validate_rollup_frame(
    df: pl.DataFrame,
    required_columns: Iterable[str],
    campaign_month: CampaignMonth,
) -> ValidationResult:
    """Apply the rules above. Returns ValidationResult; .ok False blocks write."""
    res = ValidationResult(ok=True)

    # 1. required columns
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        res.fail(f"missing required columns: {missing}")
        return res  # any downstream check would crash; stop here

    # 2. row count
    n = df.height
    if n == 0:
        res.fail("rollup is empty (0 rows)")
        return res

    # 3-6. numeric sanity
    totals = df.select(
        pl.col("volume").sum().alias("v"),
        pl.col("responders").sum().alias("r"),
        pl.col("Boards").sum().alias("b"),
        pl.col("expected_responses").sum().alias("et"),
        pl.col("expected_responses_xpm").sum().alias("ex"),
        pl.col("volume").min().alias("vmin"),
    ).row(0, named=True)

    v, r, b, et, ex, vmin = (
        totals["v"], totals["r"], totals["b"], totals["et"], totals["ex"], totals["vmin"],
    )

    if vmin is not None and vmin < 0:
        res.fail(f"volume has negative values (min={vmin})")
    if v is None or v <= 0:
        res.fail(f"total volume is non-positive ({v})")
    if r is not None and v is not None and r > v:
        res.fail(f"responders ({r}) > volume ({v})")
    if b is not None and v is not None and b > v:
        res.fail(f"Boards ({b}) > volume ({v})")
    if et is not None and v is not None and (et < 0 or et > v):
        res.warn(f"expected_responses ({et}) outside [0, volume={v}]")
    if ex is not None and v is not None and (ex < 0 or ex > v):
        res.warn(f"expected_responses_xpm ({ex}) outside [0, volume={v}]")

    # 7. campaign_month uniqueness (only if the column is present)
    if "campaign_month" in df.columns:
        unique = df["campaign_month"].unique().to_list()
        expected = campaign_month.iso
        bad = [u for u in unique if u != expected]
        if bad:
            res.fail(f"campaign_month column contains foreign values: {bad}")

    res.stats = {
        "row_count": n,
        "total_volume": v,
        "total_responders": r,
        "total_boards": b,
        "total_expected_responses": et,
        "total_expected_responses_xpm": ex,
    }
    return res


# ----------------------------------------------------------- load log persistence
def append_load_log(log_path: str | Path, row: dict) -> None:
    """Append one row to load_log.csv. Idempotent on header creation."""
    log_path = Path(log_path)
    ensure_dir(log_path.parent)
    write_header = not log_path.exists()
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOAD_LOG_COLUMNS)
        if write_header:
            w.writeheader()
        w.writerow({k: row.get(k) for k in LOAD_LOG_COLUMNS})


def make_load_log_row(
    campaign_month: CampaignMonth,
    source_file: Path,
    source_mtime: str,
    result: ValidationResult,
    status: str,
    message: str = "",
) -> dict:
    s = result.stats
    return {
        "campaign_month": campaign_month.iso,
        "source_file": str(source_file),
        "source_modified_time": source_mtime,
        "loaded_time": now_iso(),
        "row_count": s.get("row_count"),
        "total_volume": s.get("total_volume"),
        "total_responders": s.get("total_responders"),
        "total_boards": s.get("total_boards"),
        "total_expected_responses": s.get("total_expected_responses"),
        "total_expected_responses_xpm": s.get("total_expected_responses_xpm"),
        "status": status,
        "message": message,
    }


def rebuild_validation_summary(mart_dir: str | Path, out_path: str | Path) -> None:
    """Walk the mart and emit one summary row per partition currently on disk.

    This is the truth file (load_log is the history). The dashboard's Data
    Quality tab prefers this over load_log when both exist.

    A `has_xpm` boolean column is included so the DQ tab can flag months
    whose expected_rr_xpm and actual_vs_expected_xpm are unreliable (because
    the SAS pipeline didn't source EXP_RESPONSE_SCORE).
    """
    mart_dir = Path(mart_dir)
    rows = []
    for part in sorted(mart_dir.glob("campaign_month=*")):
        pq = part / "rollup.parquet"
        if not pq.exists():
            continue
        df = pl.read_parquet(pq)

        # Base totals (xpm column may or may not have real values).
        agg_exprs = [
            pl.col("volume").sum().alias("v"),
            pl.col("responders").sum().alias("r"),
            pl.col("Boards").sum().alias("b"),
            pl.col("expected_responses").sum().alias("et"),
        ]
        if "expected_responses_xpm" in df.columns:
            agg_exprs.append(pl.col("expected_responses_xpm").sum().alias("ex"))
            agg_exprs.append(
                pl.col("expected_responses_xpm").is_not_null().sum().alias("xpm_nn")
            )
        totals = df.select(agg_exprs).row(0, named=True)

        ex = totals.get("ex")
        xpm_nn = totals.get("xpm_nn", 0) or 0
        # 'has_xpm' is true only if at least one row carries a real value AND
        # the total is non-zero. All-null or all-zero is treated as missing.
        has_xpm = bool(xpm_nn) and ex is not None and ex != 0

        rows.append({
            "campaign_month": part.name.split("=", 1)[1],
            "partition_path": str(part),
            "row_count": df.height,
            "total_volume": totals["v"],
            "total_responders": totals["r"],
            "total_boards": totals["b"],
            "total_expected_responses": totals["et"],
            "total_expected_responses_xpm": ex,
            "has_xpm": has_xpm,
        })
    if not rows:
        log.warning("No mart partitions found at %s", mart_dir)
    pl.DataFrame(rows).write_csv(str(out_path))
