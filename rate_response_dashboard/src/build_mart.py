"""Parquet mart writer.

Layout:
    data/mart/rate_response_rollup/
        campaign_month=2025-01/rollup.parquet
        campaign_month=2025-02/rollup.parquet
        ...

Safe-write contract for one partition:
    1. write to data/mart/.../campaign_month=YYYY-MM__tmp/rollup.parquet
    2. caller runs validation on the temp parquet
    3. on success: atomic swap — delete old partition dir, rename __tmp → final
       on failure: delete the __tmp dir, leave existing partition untouched

The dashboard scans this directory with a glob so adding/removing partitions
needs no metadata update.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import polars as pl

from .utils import CampaignMonth, ensure_dir

log = logging.getLogger(__name__)

PARTITION_PREFIX = "campaign_month="
ROLLUP_FILENAME = "rollup.parquet"


def partition_dir(mart_dir: str | Path, cm: CampaignMonth) -> Path:
    return Path(mart_dir) / f"{PARTITION_PREFIX}{cm.iso}"


def tmp_partition_dir(mart_dir: str | Path, cm: CampaignMonth) -> Path:
    return Path(mart_dir) / f"{PARTITION_PREFIX}{cm.iso}__tmp"


def write_partition_safely(
    df: pl.DataFrame,
    mart_dir: str | Path,
    cm: CampaignMonth,
) -> Path:
    """Write df to a temp partition first, then atomically swap.

    The caller is expected to have already validated `df`. We do one final
    structural check here (row count > 0) so we never produce an empty file.
    """
    if df.is_empty():
        raise ValueError(f"refusing to write empty partition for {cm.iso}")

    mart_dir = ensure_dir(mart_dir)
    final = partition_dir(mart_dir, cm)
    tmp = tmp_partition_dir(mart_dir, cm)

    if tmp.exists():
        shutil.rmtree(tmp)
    ensure_dir(tmp)

    out_file = tmp / ROLLUP_FILENAME
    df.write_parquet(str(out_file), compression="snappy")
    log.info("wrote %s (%d rows)", out_file, df.height)

    # Atomic-ish swap. On Windows, rename over an existing dir fails, so we
    # remove the old partition first. Risk window: between rmtree and rename
    # the partition is briefly absent. Dashboards re-glob on every callback so
    # that is acceptable; we don't claim crash-safety here.
    if final.exists():
        shutil.rmtree(final)
    tmp.rename(final)
    log.info("partition %s committed", final)
    return final


def discard_tmp_partition(mart_dir: str | Path, cm: CampaignMonth) -> None:
    """Clean up after a failed validation. Idempotent."""
    tmp = tmp_partition_dir(mart_dir, cm)
    if tmp.exists():
        shutil.rmtree(tmp)
        log.info("discarded tmp partition %s", tmp)


# --------------------------------------------------------------- mart reads
# Numeric metric columns we explicitly cast to Float64 at read time so partitions
# written under different schema versions can be unioned safely.
_NUMERIC_MART_COLS = [
    "volume", "responders", "Boards",
    "expected_responses", "expected_responses_xpm",
]


def read_mart(mart_dir: str | Path) -> pl.DataFrame:
    """Scan all partitions into a single Polars DataFrame.

    Reads each partition eagerly and harmonizes numeric column dtypes
    (Int64 → Float64) before concatenating. This protects against schema
    drift across partitions — e.g. partitions ingested before the xpm/null
    handling fix stored `volume` as Int64, while newer ingests store it as
    Float64 (because the prepare step now casts unconditionally). A naive
    `pl.scan_parquet(..., hive_partitioning=True)` would fail with
    `SchemaError: data type mismatch for column volume`.

    Mart is small (O(10^4) per month × 17 months ≈ 100k rows), so the
    per-partition read cost is negligible.
    """
    mart_dir = Path(mart_dir)
    pq_files = sorted(mart_dir.glob(f"{PARTITION_PREFIX}*/{ROLLUP_FILENAME}"))
    if not pq_files:
        return pl.DataFrame()

    frames: list[pl.DataFrame] = []
    for f in pq_files:
        df = pl.read_parquet(str(f))
        # Hive partitioning would normally inject campaign_month from the path,
        # but the prepare step already added it as a column; keep a fallback.
        if "campaign_month" not in df.columns:
            cm = f.parent.name.split("=", 1)[1]
            df = df.with_columns(pl.lit(cm).alias("campaign_month"))
        for c in _NUMERIC_MART_COLS:
            if c in df.columns and df[c].dtype != pl.Float64:
                df = df.with_columns(pl.col(c).cast(pl.Float64, strict=False))
        frames.append(df)

    # diagonal_relaxed = union of all columns + dtype coercion across frames,
    # so partitions with slightly different column sets (e.g. one missing
    # expected_responses_xpm entirely) still concat cleanly.
    return pl.concat(frames, how="diagonal_relaxed")


def list_partition_months(mart_dir: str | Path) -> list[str]:
    """Return ISO months currently materialized on disk, sorted ascending."""
    mart_dir = Path(mart_dir)
    if not mart_dir.exists():
        return []
    months = []
    for p in mart_dir.glob(f"{PARTITION_PREFIX}*"):
        if not p.is_dir() or p.name.endswith("__tmp"):
            continue
        if not (p / ROLLUP_FILENAME).exists():
            continue
        months.append(p.name.split("=", 1)[1])
    return sorted(months)
