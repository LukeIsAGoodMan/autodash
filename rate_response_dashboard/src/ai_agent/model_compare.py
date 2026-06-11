"""TRM vs XPM model comparison built from rollup A/E + portfolio decile mart.

Two layers:
  • Calibration trend: A/E ratio per month for each model, sourced from the
    rollup mart's `expected_responses` (TRM) and `expected_responses_xpm`
    columns. XPM points are nulled where `has_xpm` is False so the chart
    does not lie about coverage.
  • Rank-order headline: KS / AUC / Gini / top-decile lift from the
    portfolio decile mart at the latest month. The decile binning upstream
    in SAS uses TRM10, so the headline is attributed to TRM. To produce
    XPM rank-order alongside it, SAS needs to emit a parallel decile mart
    binned on XPM07; that work is out of scope here.

When a piece of data is missing (e.g. portfolio mart empty, XPM gap), the
relevant ModelScore / CalibrationPoint is emitted with `available=False`
plus a short `note`, so the renderer can show "N/A — gap reason" rather
than a misleading 0.
"""
from __future__ import annotations

import polars as pl

from src import metrics

from .facts import CalibrationPoint, MaturityInfo, ModelComparison, ModelScore, ReportFacts


def compute(
    rollup_df: pl.DataFrame,
    decile_port_df: pl.DataFrame,
    facts: ReportFacts,
) -> ModelComparison:
    if not facts.months_in_scope:
        return ModelComparison(latest_month="", headline=[], calibration_trend=[])

    latest = facts.latest_month
    headline = _rank_order_headline(decile_port_df, latest)
    calibration = _calibration_trend(rollup_df, facts.maturity, facts.months_in_scope)
    return ModelComparison(
        latest_month=latest,
        headline=headline,
        calibration_trend=calibration,
        swap_estimate=None,
    )


# ---------------------------------------------------------------- headline


def _rank_order_headline(decile_port_df: pl.DataFrame, latest_month: str) -> list[ModelScore]:
    """KS/AUC/Gini/lift for the model whose binning produced decile_port_df.

    Assumption: portfolio decile binning is TRM10 (matches the existing
    dashboard's Rank Order tab). When SAS adds an XPM-binned decile mart
    in the future, mirror this block with model='XPM'.
    """
    trm = _score(decile_port_df, latest_month, model="TRM",
                 scope="Portfolio (TRM10 binning)")
    # XPM rank-order requires a separately binned decile mart that does not
    # exist yet; surface a clear unavailable marker rather than silently
    # omitting the row, so the renderer can prompt the user.
    xpm = ModelScore(
        model="XPM",
        campaign_month=latest_month,
        scope="Portfolio (XPM07 binning)",
        ks=None, auc=None, gini=None, top_decile_lift=None, misrank_count=None,
        available=False,
        note="Decile mart binned on XPM07 not available; ask SAS to emit "
             "exp_<MMMYY>_decile_port_xpm.csv to enable.",
    )
    return [trm, xpm]


def _score(decile_df: pl.DataFrame, month: str, model: str, scope: str) -> ModelScore:
    if decile_df.is_empty() or month not in decile_df["campaign_month"].unique().to_list():
        return ModelScore(
            model=model,  # type: ignore[arg-type]
            campaign_month=month,
            scope=scope,
            ks=None, auc=None, gini=None, top_decile_lift=None, misrank_count=None,
            available=False,
            note="Portfolio decile mart empty for this month.",
        )
    summary = metrics.decile_summary(decile_df, campaign_month=month)
    if summary.is_empty():
        return ModelScore(
            model=model,  # type: ignore[arg-type]
            campaign_month=month,
            scope=scope,
            ks=None, auc=None, gini=None, top_decile_lift=None, misrank_count=None,
            available=False,
            note="decile_summary returned no rows.",
        )
    ks = metrics.ks_value(decile_df, campaign_month=month)
    auc = metrics.auc_value(decile_df, campaign_month=month)
    gini = metrics.gini_value(decile_df, campaign_month=month)
    top_lift = float(summary["lift"][0]) if "lift" in summary.columns and summary["lift"][0] is not None else None
    misrank = int(summary["misrank"].sum()) if "misrank" in summary.columns else None
    return ModelScore(
        model=model,  # type: ignore[arg-type]
        campaign_month=month,
        scope=scope,
        ks=ks,
        auc=auc,
        gini=gini,
        top_decile_lift=top_lift,
        misrank_count=misrank,
        available=True,
        note=None,
    )


# ---------------------------------------------------------------- calibration


def _calibration_trend(
    rollup_df: pl.DataFrame,
    maturity: dict[str, MaturityInfo],
    months: list[str],
) -> list[CalibrationPoint]:
    """One CalibrationPoint per (model, month). XPM points where the month's
    has_xpm flag is False are emitted as available=False."""
    by_month = metrics.aggregate_by(rollup_df, group_dims=["campaign_month"])
    by_month_dict = {r["campaign_month"]: r for r in by_month.iter_rows(named=True)}

    out: list[CalibrationPoint] = []
    for cm in months:
        row = by_month_dict.get(cm) or {}
        ae_trm = row.get("actual_vs_expected_trm")
        out.append(CalibrationPoint(
            model="TRM",
            campaign_month=cm,
            ae_ratio=float(ae_trm) if ae_trm is not None else None,
            available=ae_trm is not None,
        ))
        info = maturity.get(cm)
        has_xpm = bool(info and info.has_xpm)
        ae_xpm = row.get("actual_vs_expected_xpm") if has_xpm else None
        out.append(CalibrationPoint(
            model="XPM",
            campaign_month=cm,
            ae_ratio=float(ae_xpm) if ae_xpm is not None else None,
            available=ae_xpm is not None,
        ))
    return out
