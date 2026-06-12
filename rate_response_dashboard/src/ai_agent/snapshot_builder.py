"""Mart → ReportFacts.

This is the only module in `ai_agent/` that reads the rollup mart. Downstream
analysis modules (mom_yoy, mix_analysis, big_mac, etc.) must consume
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


# Default slice dims used when config provides no `ai_agent.slice_dims`.
# Order matters — it's the order the renderer walks.
DEFAULT_SLICE_DIMS: tuple[str, ...] = (
    "annual_fee", "vs_band", "scorecard", "Prospect_type",
    "rm_flag", "times_mailed_12mo_cnt",
)


def build(
    rollup_df: pl.DataFrame,
    maturity: dict[str, MaturityInfo],
    cfg: dict,
    slice_dims: Iterable[str] | None = None,
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
        )

    ai_cfg = cfg.get("ai_agent", {})
    bm_cfg = ai_cfg.get("big_mac", {})
    if slice_dims is None:
        slice_dims = ai_cfg.get("slice_dims") or DEFAULT_SLICE_DIMS
    slice_dims = list(slice_dims)
    threshold = int(cfg.get("dashboard", {}).get("small_cell_threshold", 100))

    months = sorted(rollup_df["campaign_month"].unique().to_list())
    latest = months[-1]

    overall = metrics.aggregate_by(rollup_df, group_dims=["campaign_month"])
    product = metrics.aggregate_by(rollup_df, group_dims=["annual_fee", "campaign_month"])
    product = metrics.suppress_small_cells(product, threshold=threshold)

    overall_rows = [_row_to_kpi("overall", None, r) for r in overall.iter_rows(named=True)]
    product_rows = [
        _row_to_kpi("product", _str_or_none(r["annual_fee"]), r)
        for r in product.iter_rows(named=True)
    ]

    segment_trend: dict[str, list[KPIRow]] = {}
    for dim in slice_dims:
        if dim not in rollup_df.columns:
            continue
        seg = metrics.aggregate_by(rollup_df, group_dims=[dim, "campaign_month"])
        seg = metrics.suppress_small_cells(seg, threshold=threshold)
        segment_trend[dim] = [
            _row_to_kpi(dim, _str_or_none(r.get(dim)), r)
            for r in seg.iter_rows(named=True)
        ]

    # Big Mac cohort — pre-computed here because filtering raw rollup_df is
    # cheaper and cleaner than passing it around.
    bm_trend, bm_drill_trend, drill_dim = _big_mac_slices(
        rollup_df, bm_cfg, threshold
    )

    return ReportFacts(
        generated_at=now_iso(),
        latest_month=latest,
        months_in_scope=months,
        maturity={m: maturity.get(m, MaturityInfo(m, "unknown", False)) for m in months},
        overall_trend=overall_rows,
        product_trend=product_rows,
        segment_trend=segment_trend,
        big_mac_trend=bm_trend,
        big_mac_by_drill_trend=bm_drill_trend,
        big_mac_drill_dim=drill_dim,
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


# ---------------------------------------------------------------- Big Mac


def big_mac_filter_expr(bm_cfg: dict) -> pl.Expr | None:
    """Build a Polars filter expression from the ai_agent.big_mac config block.

    Returns None when the config block is empty (no Big Mac defined). Keys
    ending in `_in` are treated as set-membership checks; everything else as
    equality. Centralized here so callers don't reinvent the rule and so
    a single config edit propagates to every consumer (snapshot, big_mac.py).
    """
    if not bm_cfg:
        return None
    parts: list[pl.Expr] = []
    for key, val in bm_cfg.items():
        if key == "drill_dim":
            continue  # consumed by big_mac.py, not part of filter
        if key.endswith("_in"):
            col = key[:-3]
            parts.append(pl.col(col).is_in(list(val)))
        else:
            parts.append(pl.col(key) == val)
    if not parts:
        return None
    expr = parts[0]
    for p in parts[1:]:
        expr = expr & p
    return expr


def _big_mac_slices(
    rollup_df: pl.DataFrame, bm_cfg: dict, threshold: int,
) -> tuple[list[KPIRow], list[KPIRow], str | None]:
    expr = big_mac_filter_expr(bm_cfg)
    drill_dim = bm_cfg.get("drill_dim") if bm_cfg else None
    if expr is None:
        return [], [], drill_dim
    # Skip filter columns the mart doesn't have rather than crashing.
    missing = [k for k in bm_cfg if k not in ("drill_dim",)
               and (k[:-3] if k.endswith("_in") else k) not in rollup_df.columns]
    if missing:
        return [], [], drill_dim
    subset = rollup_df.filter(expr)
    if subset.is_empty():
        return [], [], drill_dim

    bm_overall = metrics.aggregate_by(subset, group_dims=["campaign_month"])
    bm_trend = [_row_to_kpi("big_mac", None, r) for r in bm_overall.iter_rows(named=True)]

    bm_by_drill: list[KPIRow] = []
    if drill_dim and drill_dim in subset.columns:
        bm_drill = metrics.aggregate_by(subset, group_dims=[drill_dim, "campaign_month"])
        bm_drill = metrics.suppress_small_cells(bm_drill, threshold=threshold)
        bm_by_drill = [
            _row_to_kpi(f"big_mac/{drill_dim}", _str_or_none(r.get(drill_dim)), r)
            for r in bm_drill.iter_rows(named=True)
        ]
    return bm_trend, bm_by_drill, drill_dim


# ---------------------------------------------------------------- helpers


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
