"""Mart → ReportFacts.

This is the only module in `ai_agent/` that reads the rollup mart. Downstream
analysis modules (mom_yoy, model_compare, future LLM agents) must consume
ReportFacts and not touch parquet directly — that keeps the I/O surface
small and the analysis layer trivially testable with in-memory fixtures.

Aggregation always delegates to `src.metrics.aggregate_by`, which enforces
the sum/sum rate rule. Small-cell suppression is applied to per-segment
slices (rates only — counts are kept so the table still shows sample size).
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

import polars as pl

from src import metrics
from src.utils import now_iso

from .facts import KPIRow, MaturityInfo, ReportFacts


# Segment dims to surface in `ReportFacts.segment_latest`. Order matters —
# it's the order the renderer walks. Keep this list pragmatic: dims with
# enough cardinality to be useful but not so much they overwhelm a report.
DEFAULT_SEGMENT_DIMS: tuple[str, ...] = (
    "vs_band",
    "scorecard",
    "Prospect_type",
    "rm_flag",
)


def build(
    rollup_df: pl.DataFrame,
    maturity: dict[str, MaturityInfo],
    cfg: dict,
    segment_dims: Iterable[str] = DEFAULT_SEGMENT_DIMS,
) -> ReportFacts:
    """Aggregate the rollup mart into a ReportFacts bundle.

    `maturity` is loaded separately (see `load_maturity`) so this function
    stays pure — same DataFrame in, same facts out.
    """
    if rollup_df.is_empty():
        return ReportFacts(
            generated_at=now_iso(),
            latest_month="",
            months_in_scope=[],
            maturity={},
            overall_trend=[],
            product_trend=[],
            segment_latest={},
        )

    threshold = int(cfg.get("dashboard", {}).get("small_cell_threshold", 100))

    months = sorted(rollup_df["campaign_month"].unique().to_list())
    latest = months[-1]

    overall = metrics.aggregate_by(rollup_df, group_dims=["campaign_month"])
    product = metrics.aggregate_by(rollup_df, group_dims=["annual_fee", "campaign_month"])
    # Small-cell suppression for product trend rows — a tiny product slice
    # would have noisy rates; counts are kept.
    product = metrics.suppress_small_cells(product, threshold=threshold)

    overall_rows = [_row_to_kpi("overall", None, r) for r in overall.iter_rows(named=True)]
    product_rows = [
        _row_to_kpi("product", r["annual_fee"], r) for r in product.iter_rows(named=True)
    ]

    prior = months[-2] if len(months) >= 2 else None
    latest_df = rollup_df.filter(pl.col("campaign_month") == latest)
    prior_df = (
        rollup_df.filter(pl.col("campaign_month") == prior) if prior else None
    )
    segment_latest: dict[str, list[KPIRow]] = {}
    segment_prior: dict[str, list[KPIRow]] = {}
    for dim in segment_dims:
        if dim not in latest_df.columns:
            continue
        seg = metrics.aggregate_by(latest_df, group_dims=[dim, "campaign_month"])
        seg = metrics.suppress_small_cells(seg, threshold=threshold)
        segment_latest[dim] = [
            _row_to_kpi(dim, _str_or_none(r.get(dim)), r) for r in seg.iter_rows(named=True)
        ]
        if prior_df is not None:
            segp = metrics.aggregate_by(prior_df, group_dims=[dim, "campaign_month"])
            segp = metrics.suppress_small_cells(segp, threshold=threshold)
            segment_prior[dim] = [
                _row_to_kpi(dim, _str_or_none(r.get(dim)), r)
                for r in segp.iter_rows(named=True)
            ]

    return ReportFacts(
        generated_at=now_iso(),
        latest_month=latest,
        months_in_scope=months,
        maturity={m: maturity.get(m, MaturityInfo(m, "unknown", False)) for m in months},
        overall_trend=overall_rows,
        product_trend=product_rows,
        segment_latest=segment_latest,
        segment_prior=segment_prior,
    )


def load_maturity(cfg: dict) -> dict[str, MaturityInfo]:
    """Read maturity + has_xpm from the existing validation_summary.csv.

    Returns an empty dict when the summary file is missing (fresh local
    install). Downstream code falls back to 'unknown' maturity in that case,
    which the renderer surfaces as a yellow warning rather than silently
    treating immature data as final.
    """
    logs_dir = Path(cfg.get("paths", {}).get("logs_dir", "./data/logs"))
    summary_path = logs_dir / "validation_summary.csv"
    if not summary_path.exists():
        return {}
    out: dict[str, MaturityInfo] = {}
    with summary_path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            cm = (row.get("campaign_month") or "").strip()
            if not cm:
                continue
            status = (row.get("maturity_status") or "unknown").strip() or "unknown"
            if status not in ("full", "partial", "unknown"):
                status = "unknown"
            has_xpm = (row.get("has_xpm") or "").strip().lower() in ("1", "true", "yes")
            out[cm] = MaturityInfo(campaign_month=cm, status=status, has_xpm=has_xpm)
    return out


def _row_to_kpi(dim: str, dim_value: str | None, r: dict) -> KPIRow:
    return KPIRow(
        dim=dim,
        dim_value=dim_value,
        campaign_month=r["campaign_month"],
        volume=_f(r.get("volume")),
        responders=_f(r.get("responders")),
        boards=_f(r.get("Boards")),
        nrr=_f(r.get("actual_response_rate")),
        board_rate=_f(r.get("actual_board_rate")),
        expected_rr_trm=_f(r.get("expected_rr_trm")),
        expected_rr_xpm=_f(r.get("expected_rr_xpm")),
        ae_trm=_f(r.get("actual_vs_expected_trm")),
        ae_xpm=_f(r.get("actual_vs_expected_xpm")),
    )


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _str_or_none(v) -> str | None:
    if v is None:
        return None
    return str(v)
