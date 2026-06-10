"""Metric definitions.

The single load-bearing rule of this module: rates are computed from
sum(numerator) / sum(denominator). We never average a rate column. The SAS
precomputed GRR / NRR columns are dropped at ingest time precisely so the
dashboard cannot accidentally reach for them.

All functions operate on Polars DataFrames since the mart is small once
aggregated (millions of rollup rows at most, usually < 1M).
"""
from __future__ import annotations

from typing import Iterable

import polars as pl

# Column names exactly as they appear in the parquet mart.
VOLUME = "volume"
RESPONDERS = "responders"
BOARDS = "Boards"
EXP_TRM = "expected_responses"
EXP_XPM = "expected_responses_xpm"

# Metrics the UI can ask for. Keep this list and config.catalog.metrics in sync.
METRIC_COLUMNS = [
    "volume",
    "responders",
    "Boards",
    "actual_board_rate",
    "actual_response_rate",
    "expected_rr_trm",
    "expected_rr_xpm",
    "actual_vs_expected_trm",
    "actual_vs_expected_xpm",
]


def _safe_div(num: pl.Expr, den: pl.Expr) -> pl.Expr:
    """Return num/den, or null when den is 0/null. Avoids divide-by-zero noise."""
    return pl.when(den.is_null() | (den == 0)).then(None).otherwise(num / den)


_XPM_NN = "__xpm_nn"   # helper column: count of non-null xpm rows per group


def aggregate_by(
    df: pl.DataFrame,
    group_dims: Iterable[str],
) -> pl.DataFrame:
    """Aggregate the mart over arbitrary dimensions, then derive rates.

    The aggregation also tracks how many rows in each group carried a
    non-null expected_responses_xpm so add_rate_columns can null-out the
    XPM rate for groups that had no xpm data (rather than reporting 0).
    """
    group_dims = list(group_dims)

    agg = df.group_by(group_dims).agg(
        pl.col(VOLUME).sum().alias(VOLUME),
        pl.col(RESPONDERS).sum().alias(RESPONDERS),
        pl.col(BOARDS).sum().alias(BOARDS),
        pl.col(EXP_TRM).sum().alias(EXP_TRM),
        pl.col(EXP_XPM).sum().alias(EXP_XPM),
        pl.col(EXP_XPM).is_not_null().sum().alias(_XPM_NN),
    )

    return add_rate_columns(agg).drop(_XPM_NN).sort(group_dims)


def add_rate_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Append the six derived rate columns to an aggregated frame.

    When the helper column `__xpm_nn` is present, expected_rr_xpm and
    actual_vs_expected_xpm are nulled for groups with no non-null xpm
    rows (avoids confusing "0%" for months that simply don't have data).
    """
    df = df.with_columns(
        actual_response_rate=_safe_div(pl.col(RESPONDERS), pl.col(VOLUME)),
        actual_board_rate=_safe_div(pl.col(BOARDS), pl.col(VOLUME)),
        expected_rr_trm=_safe_div(pl.col(EXP_TRM), pl.col(VOLUME)),
        expected_rr_xpm=_safe_div(pl.col(EXP_XPM), pl.col(VOLUME)),
    )
    if _XPM_NN in df.columns:
        df = df.with_columns(
            expected_rr_xpm=pl.when(pl.col(_XPM_NN) > 0)
                              .then(pl.col("expected_rr_xpm"))
                              .otherwise(None),
        )
    return df.with_columns(
        actual_vs_expected_trm=_safe_div(pl.col("actual_response_rate"),
                                         pl.col("expected_rr_trm")),
        actual_vs_expected_xpm=_safe_div(pl.col("actual_response_rate"),
                                         pl.col("expected_rr_xpm")),
    )


def kpi_totals(df: pl.DataFrame) -> dict:
    """Single-row KPIs for the Executive Summary tab."""
    if df.is_empty():
        return {k: None for k in METRIC_COLUMNS}
    agg = aggregate_by(df, group_dims=[pl.lit(1).alias("_one")]).drop("_one")
    row = agg.row(0, named=True)
    return {k: row.get(k) for k in METRIC_COLUMNS}


def monthly_trend(df: pl.DataFrame) -> pl.DataFrame:
    """Time series across campaign_month for KPI trend charts."""
    return aggregate_by(df, group_dims=["campaign_month"])


def pivot_table(
    df: pl.DataFrame,
    row_dim: str,
    metric: str,
    column_dim: str = "campaign_month",
) -> pl.DataFrame:
    """Long → wide pivot. row_dim x column_dim with one metric in cells.

    Aggregation is done BEFORE pivoting so the metric is correctly recomputed
    from sums at each (row_dim, column_dim) cell.
    """
    long = aggregate_by(df, group_dims=[row_dim, column_dim])
    wide = long.pivot(
        values=metric,
        index=row_dim,
        on=column_dim,
        aggregate_function="first",  # already aggregated above; just lay it out
    )
    return wide.sort(row_dim)


def suppress_small_cells(
    df: pl.DataFrame,
    volume_col: str = VOLUME,
    threshold: int = 100,
    metrics_to_mask: Iterable[str] = (
        "actual_response_rate",
        "actual_board_rate",
        "expected_rr_trm",
        "expected_rr_xpm",
        "actual_vs_expected_trm",
        "actual_vs_expected_xpm",
    ),
) -> pl.DataFrame:
    """Null out rate columns where volume is below threshold.

    Counts (volume, responders, Boards) are kept so the table still reports
    sample size; only derived rates are masked.
    """
    if threshold <= 0 or volume_col not in df.columns:
        return df
    present = [m for m in metrics_to_mask if m in df.columns]
    if not present:
        return df
    return df.with_columns(
        [
            pl.when(pl.col(volume_col) < threshold)
            .then(None)
            .otherwise(pl.col(m))
            .alias(m)
            for m in present
        ]
    )
