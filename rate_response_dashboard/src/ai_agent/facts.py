"""Data models for the report pipeline.

These are plain dataclasses, not Pydantic models — Stage 1 produces and
consumes them all in-process, with no JSON boundary that needs schema
validation. When Stage 2 introduces LLM agents, the JSON-facing types will
live in a separate `schemas.py` and be defined with Pydantic; the
dataclasses here stay pure-Python.

Naming follows the existing mart columns where possible (volume, responders,
Boards, expected_rr_trm, etc.) to make joins/lookups by name natural.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


MaturityStatus = Literal["full", "partial", "unknown"]


@dataclass(frozen=True)
class MaturityInfo:
    """Per-campaign-month reportability flags. Maturity comes from the existing
    validation_summary (sas_run_date vs end_of_month + threshold). `has_xpm`
    tracks the SAS-side EXP_RESPONSE_SCORE gap — when false the XPM metric
    columns are unreliable for that month and must be presented as N/A."""
    campaign_month: str
    status: MaturityStatus
    has_xpm: bool


@dataclass(frozen=True)
class KPIRow:
    """One aggregated KPI bundle at (dim_value, campaign_month).

    `dim` names the slice axis (e.g. 'product' / 'vs_band' / 'overall') and
    `dim_value` carries the slice value (e.g. '$75/$99'). For overall rollups
    `dim_value` is None. Rates are pre-recomputed from sums by aggregate_by()
    in src.metrics — never average these across rows.
    """
    dim: str
    dim_value: str | None
    campaign_month: str
    volume: float | None
    responders: float | None
    boards: float | None
    nrr: float | None
    board_rate: float | None
    expected_rr_trm: float | None
    expected_rr_xpm: float | None
    ae_trm: float | None
    ae_xpm: float | None


@dataclass
class ReportFacts:
    """Output of snapshot_builder. Everything downstream consumes this and
    must NOT touch the marts directly."""
    generated_at: str
    latest_month: str
    months_in_scope: list[str]
    maturity: dict[str, MaturityInfo]
    overall_trend: list[KPIRow]
    product_trend: list[KPIRow]
    segment_latest: dict[str, list[KPIRow]] = field(default_factory=dict)
    # Prior-month slice for each segment dim, used by mom_yoy to compute
    # segment-level MoM. Keys mirror segment_latest. Empty when no prior month.
    segment_prior: dict[str, list[KPIRow]] = field(default_factory=dict)


# ============================================================================
# MoM / YoY analysis
# ============================================================================

Direction = Literal["up", "down", "flat"]


@dataclass(frozen=True)
class Movement:
    """A single MoM or YoY change in a single metric on a single slice."""
    dim: str
    dim_value: str | None
    metric: str             # 'nrr' / 'board_rate' / 'volume' / ...
    period: Literal["MoM", "YoY"]
    current_month: str
    prior_month: str
    current_value: float | None
    prior_value: float | None
    delta_abs: float | None       # value - prior_value  (in raw units; rate is in pct points)
    delta_bps: float | None       # rounded basis points for rate metrics; None for counts
    delta_pct: float | None       # (value - prior) / prior; for count metrics
    direction: Direction


@dataclass
class MoMYoYAnalysis:
    """Output of mom_yoy.compute(). Surfaces overall + per-product movements
    plus the top-K biggest segment movers for the latest month."""
    latest_month: str
    overall_mom: list[Movement]            # one per metric of interest
    overall_yoy: list[Movement]
    product_mom: list[Movement]            # one per (product × metric)
    product_yoy: list[Movement]
    biggest_movers: list[Movement] = field(default_factory=list)


# ============================================================================
# Model comparison (TRM vs XPM) — built from decile mart + rollup A/E
# ============================================================================


@dataclass(frozen=True)
class ModelScore:
    """Headline rank-order metrics for one model on one month/scope."""
    model: Literal["TRM", "XPM"]
    campaign_month: str
    scope: str                       # human-readable: 'Portfolio' or 'Scorecard=12'
    ks: float | None
    auc: float | None
    gini: float | None
    top_decile_lift: float | None
    misrank_count: int | None        # number of decile rows flagged misranked
    available: bool                  # False when source data missing (e.g. XPM gap)
    note: str | None = None          # short reason when unavailable


@dataclass(frozen=True)
class CalibrationPoint:
    """A/E ratio at one campaign_month. ratio close to 1.0 = well-calibrated."""
    model: Literal["TRM", "XPM"]
    campaign_month: str
    ae_ratio: float | None
    available: bool


@dataclass
class ModelComparison:
    """Output of model_compare.compute(). Side-by-side TRM and XPM."""
    latest_month: str
    headline: list[ModelScore]                  # both models, latest month
    calibration_trend: list[CalibrationPoint]   # both models across all months
    # Reserved for Stage C — counterfactual "switch model" estimate. Stage 1
    # leaves this empty; we wire the field now so the template doesn't change.
    swap_estimate: dict | None = None


# ============================================================================
# Top-level package handed to the renderer
# ============================================================================


@dataclass
class ReportPackage:
    """Everything the HTML/PPT renderer needs to produce a report."""
    facts: ReportFacts
    mom_yoy: MoMYoYAnalysis
    model: ModelComparison
    config_snapshot: dict = field(default_factory=dict)
