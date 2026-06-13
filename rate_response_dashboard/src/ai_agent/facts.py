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
    must NOT touch the marts directly.

    `segment_trend[dim]` is the full multi-month slice for `dim` — analysis
    modules (mom_yoy, mix_analysis, slice_trends) all derive what they need
    from here so we keep mart reads to one place.
    """
    generated_at: str
    latest_month: str
    months_in_scope: list[str]
    maturity: dict[str, MaturityInfo]
    overall_trend: list[KPIRow]
    product_trend: list[KPIRow]
    segment_trend: dict[str, list[KPIRow]] = field(default_factory=dict)
    # Big Mac cohort facts — None when filter rule yields zero rows.
    big_mac_trend: list[KPIRow] = field(default_factory=list)
    big_mac_by_drill_trend: list[KPIRow] = field(default_factory=list)
    big_mac_drill_dim: str | None = None


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
    delta_abs: float | None       # value - prior_value (raw units; rate is in pct points)
    delta_bps: float | None       # rounded basis points for rate metrics; None for counts
    delta_pct: float | None       # (value - prior) / prior; for count metrics
    direction: Direction


@dataclass
class MoMYoYAnalysis:
    """Output of mom_yoy.compute(). Surfaces overall + per-product movements
    plus the top-K biggest segment movers for the latest month."""
    latest_month: str
    overall_mom: list[Movement]
    overall_yoy: list[Movement]
    product_mom: list[Movement]
    product_yoy: list[Movement]
    biggest_movers: list[Movement] = field(default_factory=list)


# ============================================================================
# Mix analysis — population share shifts
# ============================================================================


@dataclass(frozen=True)
class MixShare:
    """Share of total volume that a single dim_value commands in one month."""
    dim: str
    dim_value: str | None
    campaign_month: str
    volume: float | None
    share: float | None   # volume / total_volume_that_month  (0..1)


@dataclass(frozen=True)
class MixShiftRow:
    """How a dim_value's share moved between two months."""
    dim: str
    dim_value: str | None
    current_month: str
    prior_month: str
    current_share: float | None
    prior_share: float | None
    delta_share_pp: float | None   # in percentage points (e.g. +1.5)
    direction: Direction


@dataclass
class MixAnalysis:
    """One MixShiftRow per (dim, dim_value) for latest vs prior month.
    `top_shifts` is the union of biggest +/- absolute movers across all dims,
    capped at top_k. Useful for narrative bullets."""
    latest_month: str
    prior_month: str | None
    by_dim: dict[str, list[MixShiftRow]] = field(default_factory=dict)
    top_shifts: list[MixShiftRow] = field(default_factory=list)


# ============================================================================
# Slice trends — multi-month per-dim NRR / board_rate trends for charts
# ============================================================================


@dataclass
class SliceTrendBundle:
    """For each dim, the multi-month KPIRows; the renderer/chart_builder
    walks this to produce one chart per dim. Carries no narrative.

    Subset to `lookback_months` to keep charts readable.
    """
    months: list[str]
    by_dim: dict[str, list[KPIRow]] = field(default_factory=dict)


# ============================================================================
# Big Mac drill-down
# ============================================================================


@dataclass
class BigMacAnalysis:
    """Big Mac cohort facts + which drill_dim slice moved most MoM."""
    filter_summary: dict           # filter rule literal, for transparency
    drill_dim: str | None
    overall_trend: list[KPIRow]    # cohort total per month
    by_drill_trend: list[KPIRow]   # (drill_dim_value, month)
    biggest_drop: Movement | None  # worst NRR mover MoM within the drill dim
    biggest_gain: Movement | None
    cohort_empty: bool = False


# ============================================================================
# Model can-it-catch the change
# ============================================================================


@dataclass(frozen=True)
class ModelCatchRow:
    """For one observed mover (e.g. a vs_band that dropped 12 bps), how did
    the two models' expected NRR predict it?

    A model "caught" the change if its expected_rr moved in the same
    direction and within roughly the same magnitude. A model "missed" if
    expected was flat (or moved opposite) while actual moved materially.
    """
    dim: str
    dim_value: str | None
    period: Literal["MoM", "YoY"]
    current_month: str
    prior_month: str

    actual_delta_bps: float | None
    trm_expected_delta_bps: float | None
    xpm_expected_delta_bps: float | None

    # Verdict per model. "match" = same direction and within 2x magnitude;
    # "partial" = same direction but off magnitude; "miss" = wrong direction
    # or model was flat (<1bp) when actual was material; "n/a" = expected
    # missing for that month.
    trm_verdict: Literal["match", "partial", "miss", "n/a"]
    xpm_verdict: Literal["match", "partial", "miss", "n/a"]


@dataclass
class ModelCatchAnalysis:
    rows: list[ModelCatchRow] = field(default_factory=list)
    # Summary count by verdict for each model, for narrative.
    trm_summary: dict[str, int] = field(default_factory=dict)
    xpm_summary: dict[str, int] = field(default_factory=dict)


# ============================================================================
# Top combination movers — multi-dim cohort discovery (Section D)
# ============================================================================


@dataclass(frozen=True)
class ComboMovement:
    """A single multi-dim cell's MoM NRR move.

    `dim_values` is keyed by dimension name and ordered to match the
    `dim_pair` string. Volume is reported for the latest month — the prior
    month's volume is used only for the small-cell filter, not the display.
    """
    dim_pair: str                      # e.g. "annual_fee × vs_band"
    dim_values: dict[str, object]      # {"annual_fee": "$75 / $99", "vs_band": "550-600"}
    current_month: str
    prior_month: str
    current_nrr: float
    prior_nrr: float
    delta_bps: float
    current_volume: float              # for context — large cells matter more
    direction: Direction


@dataclass
class TopCombinationAnalysis:
    """Section D facts. `top_gainers` and `top_losers` are mutually exclusive
    halves of the ranked candidate list, so a combo never appears in both."""
    latest_month: str
    prior_month: str | None
    min_volume: int
    pairs_evaluated: list[str]
    top_gainers: list[ComboMovement] = field(default_factory=list)
    top_losers: list[ComboMovement] = field(default_factory=list)


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
    misrank_count: int | None
    available: bool
    note: str | None = None


@dataclass(frozen=True)
class CalibrationPoint:
    model: Literal["TRM", "XPM"]
    campaign_month: str
    ae_ratio: float | None
    available: bool


@dataclass
class ModelComparison:
    latest_month: str
    headline: list[ModelScore]
    calibration_trend: list[CalibrationPoint]
    swap_estimate: dict | None = None


# ============================================================================
# Top-level package handed to the renderer
# ============================================================================


@dataclass
class ReportPackage:
    facts: ReportFacts
    mom_yoy: MoMYoYAnalysis
    mix: MixAnalysis
    slice_trends: SliceTrendBundle
    big_mac: BigMacAnalysis
    combinations: TopCombinationAnalysis
    model_catch: ModelCatchAnalysis
    model: ModelComparison
    config_snapshot: dict = field(default_factory=dict)
    # Base64-encoded PNG charts, keyed by stable chart id. The Jinja
    # template references these by name; missing entries render as
    # "[chart unavailable]" placeholders rather than broken images.
    charts: dict[str, str] = field(default_factory=dict)
    # LLM-generated commentary slots, keyed by slot_id (chart name or
    # section summary id like 'section_a_summary'). Empty dict = Stage 1
    # mode (no LLM); template falls back to the "Commentary pending" stub.
    commentary: dict = field(default_factory=dict)
    # Audit pass findings, keyed by section letter ('A', 'B', 'C', ...).
    # Each entry is a list of AuditIssue (from llm.auditor). Empty when
    # the audit step is disabled or no issues found. Schema kept loose
    # (dict, not Pydantic) so this dataclass doesn't pull in LLM-layer
    # types — the renderer reads issue fields by attribute access.
    audit_findings: dict = field(default_factory=dict)
