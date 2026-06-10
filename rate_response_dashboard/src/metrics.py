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


# ============================================================================
# Decile-grain metrics (P4): KS, capture rate, lift.
# Operate on the decile mart frame: columns
#   campaign_month, scorecard, total_decile, volume, responders, Boards
# Convention: decile 1 = highest model score (most likely responders).
# ============================================================================

def decile_summary(
    df: pl.DataFrame,
    scorecard: int | None = None,
    campaign_month: str | None = None,
) -> pl.DataFrame:
    """Return a per-decile summary with cumulative capture and lift.

    Filters by `scorecard` and/or `campaign_month` if provided. Sums across
    everything else (so e.g. multi-month aggregation works when both are None).
    """
    out = df
    if scorecard is not None and "scorecard" in out.columns:
        out = out.filter(pl.col("scorecard") == scorecard)
    if campaign_month is not None and "campaign_month" in out.columns:
        out = out.filter(pl.col("campaign_month") == campaign_month)
    if out.is_empty():
        return pl.DataFrame()

    by_dec = out.group_by("total_decile").agg(
        pl.col("volume").sum().alias("volume"),
        pl.col("responders").sum().alias("responders"),
        pl.col("Boards").sum().alias("Boards"),
    ).sort("total_decile")

    total_v = by_dec["volume"].sum() or 0
    total_r = by_dec["responders"].sum() or 0
    overall_rr = (total_r / total_v) if total_v else None

    by_dec = by_dec.with_columns(
        response_rate=_safe_div(pl.col("responders"), pl.col("volume")),
        non_responders=(pl.col("volume") - pl.col("responders")),
    ).with_columns(
        cum_volume=pl.col("volume").cum_sum(),
        cum_responders=pl.col("responders").cum_sum(),
        cum_non_responders=pl.col("non_responders").cum_sum(),
    )
    by_dec = by_dec.with_columns(
        cum_volume_pct=_safe_div(pl.col("cum_volume"), pl.lit(total_v)),
        cum_capture=_safe_div(pl.col("cum_responders"), pl.lit(total_r)),
        cum_non_resp_pct=_safe_div(pl.col("cum_non_responders"),
                                   pl.lit(total_v - total_r)),
    )
    if overall_rr is not None and overall_rr > 0:
        by_dec = by_dec.with_columns(
            lift=pl.col("response_rate") / overall_rr,
        )
    else:
        by_dec = by_dec.with_columns(lift=pl.lit(None).cast(pl.Float64))
    return by_dec


def ks_value(
    df: pl.DataFrame,
    scorecard: int | None = None,
    campaign_month: str | None = None,
) -> float | None:
    """KS = max( |cum_responders_pct − cum_non_responders_pct| ) across deciles.

    Returns None when there is not enough data to compute (no responders or
    no non-responders).
    """
    s = decile_summary(df, scorecard=scorecard, campaign_month=campaign_month)
    if s.is_empty():
        return None
    if "cum_capture" not in s.columns or "cum_non_resp_pct" not in s.columns:
        return None
    spread = (s["cum_capture"] - s["cum_non_resp_pct"]).abs()
    mx = spread.max()
    if mx is None:
        return None
    return float(mx)


def ks_by_month(df: pl.DataFrame, scorecard: int | None = None) -> pl.DataFrame:
    """KS per campaign_month for the given scorecard (or across all)."""
    if "campaign_month" not in df.columns:
        return pl.DataFrame()
    months = df["campaign_month"].unique().sort().to_list()
    rows = []
    for m in months:
        rows.append({
            "campaign_month": m,
            "ks": ks_value(df, scorecard=scorecard, campaign_month=m),
        })
    return pl.DataFrame(rows)


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
