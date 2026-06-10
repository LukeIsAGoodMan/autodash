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
# Decile-grain metrics (P4): KS, capture rate, lift, misrank, AUC, Gini.
#
# Two mart shapes are supported via the `decile_col` parameter:
#   - scorecard-level: columns campaign_month, scorecard, decile (1..10),
#     volume, responders, Boards.  Pass decile_col="decile", scorecard=<int>.
#   - portfolio-level: columns campaign_month, decile (1..20),
#     volume, responders, Boards.  Pass decile_col="decile", scorecard=None.
#
# Convention: decile 1 = highest model score (most likely responders).
# ============================================================================
DECILE_COL = "decile"


def decile_summary(
    df: pl.DataFrame,
    scorecard: int | None = None,
    campaign_month: str | None = None,
    decile_col: str = DECILE_COL,
) -> pl.DataFrame:
    """Return per-decile summary: counts, rates, cum_capture, lift, KS, misrank.

    `misrank` = 1 when this decile's response_rate is *higher* than the
    previous decile's. For a well-behaved rank ordering (decile 1 should be
    the best) we expect strict decrease; any 1 in this column flags a
    rank-order violation between adjacent bins. The top decile (row 0) is
    always 0 since there's nothing to compare it to.

    `per_decile_ks` = |cum_capture − cum_non_resp_pct| at each decile; the
    KS statistic is the max of this column.
    """
    out = df
    if scorecard is not None and "scorecard" in out.columns:
        out = out.filter(pl.col("scorecard") == scorecard)
    if campaign_month is not None and "campaign_month" in out.columns:
        out = out.filter(pl.col("campaign_month") == campaign_month)
    if out.is_empty() or decile_col not in out.columns:
        return pl.DataFrame()

    by_dec = (
        out.group_by(decile_col)
           .agg(
               pl.col("volume").sum().alias("volume"),
               pl.col("responders").sum().alias("responders"),
               pl.col("Boards").sum().alias("Boards"),
           )
           .sort(decile_col)
    )

    total_v = by_dec["volume"].sum() or 0
    total_r = by_dec["responders"].sum() or 0
    total_nr = (total_v - total_r) if total_v else 0
    overall_rr = (total_r / total_v) if total_v else None

    by_dec = by_dec.with_columns(
        response_rate=_safe_div(pl.col("responders"), pl.col("volume")),
        non_responders=(pl.col("volume") - pl.col("responders")),
    ).with_columns(
        cum_volume=pl.col("volume").cum_sum(),
        cum_responders=pl.col("responders").cum_sum(),
        cum_non_responders=pl.col("non_responders").cum_sum(),
    ).with_columns(
        cum_volume_pct=_safe_div(pl.col("cum_volume"), pl.lit(total_v)),
        cum_capture=_safe_div(pl.col("cum_responders"), pl.lit(total_r)),
        cum_non_resp_pct=_safe_div(pl.col("cum_non_responders"), pl.lit(total_nr)),
    ).with_columns(
        # Per-decile KS contribution (the table-wide KS is the max).
        per_decile_ks=(pl.col("cum_capture") - pl.col("cum_non_resp_pct")).abs(),
        # Misrank: this row's response_rate exceeds the previous row's (expected
        # to be lower since decile 1 is best). Use shift; the top decile gets 0.
        misrank=(
            pl.col("response_rate") > pl.col("response_rate").shift(1)
        ).cast(pl.Int8).fill_null(0),
    )
    if overall_rr is not None and overall_rr > 0:
        by_dec = by_dec.with_columns(lift=pl.col("response_rate") / overall_rr)
    else:
        by_dec = by_dec.with_columns(lift=pl.lit(None).cast(pl.Float64))
    return by_dec


def ks_value(
    df: pl.DataFrame,
    scorecard: int | None = None,
    campaign_month: str | None = None,
    decile_col: str = DECILE_COL,
) -> float | None:
    """KS = max(|cum_capture − cum_non_resp_pct|) across deciles."""
    s = decile_summary(df, scorecard, campaign_month, decile_col)
    if s.is_empty() or "per_decile_ks" not in s.columns:
        return None
    mx = s["per_decile_ks"].max()
    return float(mx) if mx is not None else None


def auc_value(
    df: pl.DataFrame,
    scorecard: int | None = None,
    campaign_month: str | None = None,
    decile_col: str = DECILE_COL,
) -> float | None:
    """Trapezoidal AUC under the ROC curve.

    ROC: x = cumulative non-responder fraction, y = cumulative responder
    fraction. We anchor at (0,0) and walk the cumulative arrays in decile
    order, summing trapezoid areas. With perfect rank ordering AUC → 1.0;
    random rank ordering → 0.5; perfectly reversed → 0.0.
    """
    s = decile_summary(df, scorecard, campaign_month, decile_col)
    if s.is_empty() or "cum_non_resp_pct" not in s.columns:
        return None
    xs = [0.0] + s["cum_non_resp_pct"].to_list()
    ys = [0.0] + s["cum_capture"].to_list()
    if any(v is None for v in xs + ys):
        return None
    auc = 0.0
    for i in range(1, len(xs)):
        auc += (xs[i] - xs[i - 1]) * (ys[i] + ys[i - 1]) / 2
    return float(auc)


def gini_value(
    df: pl.DataFrame,
    scorecard: int | None = None,
    campaign_month: str | None = None,
    decile_col: str = DECILE_COL,
) -> float | None:
    """Gini = 2 × AUC − 1.  Range [-1, 1]; perfect = 1, random = 0."""
    auc = auc_value(df, scorecard, campaign_month, decile_col)
    if auc is None:
        return None
    return 2 * auc - 1


def ks_by_month(
    df: pl.DataFrame,
    scorecard: int | None = None,
    decile_col: str = DECILE_COL,
) -> pl.DataFrame:
    """KS per campaign_month for the given scorecard (or across all)."""
    if "campaign_month" not in df.columns:
        return pl.DataFrame()
    months = df["campaign_month"].unique().sort().to_list()
    rows = []
    for m in months:
        rows.append({
            "campaign_month": m,
            "ks": ks_value(df, scorecard=scorecard, campaign_month=m,
                           decile_col=decile_col),
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
