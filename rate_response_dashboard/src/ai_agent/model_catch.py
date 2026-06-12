"""For each top mover identified by mom_yoy, did the expected_rr move with it?

This is the bridge between "what changed" (mom_yoy) and "which model saw
it coming" (model_compare). For each biggest mover at the segment level,
we look up the corresponding expected_rr_trm and expected_rr_xpm from the
same segment slice and ask:

  • did expected move in the same direction as actual?
  • did the magnitude line up?

A model that captures the change is doing its job. A model that misses
material movement is a calibration drift signal.

Verdict thresholds are deliberately conservative — we'd rather call a
borderline case "partial" than swing for "match" / "miss" and mislead
the analyst. All thresholds in basis points.
"""
from __future__ import annotations

from typing import Literal

from .facts import ModelCatchAnalysis, ModelCatchRow, MoMYoYAnalysis, ReportFacts


# Below this magnitude, an "actual" change is considered noise and the row
# is dropped from the analysis entirely — we don't want to score model
# performance on movements smaller than the model itself can resolve.
_ACTUAL_MATERIAL_BPS = 2.0

# Expected change below this magnitude counts as "model said no change".
_EXPECTED_FLAT_BPS = 1.0

# Match if expected magnitude is within this ratio of actual magnitude.
_MATCH_MAGNITUDE_TOL = 0.5


def compute(facts: ReportFacts, mm: MoMYoYAnalysis) -> ModelCatchAnalysis:
    if not mm.biggest_movers or not facts.segment_trend:
        return ModelCatchAnalysis()

    rows: list[ModelCatchRow] = []
    for mover in mm.biggest_movers:
        if mover.metric != "nrr" or mover.period != "MoM":
            continue
        if mover.delta_bps is None or abs(mover.delta_bps) < _ACTUAL_MATERIAL_BPS:
            continue
        # Look up the segment slice for this dim to get expected rates.
        slice_rows = facts.segment_trend.get(mover.dim) or []
        cur_row = next(
            (r for r in slice_rows
             if r.campaign_month == mover.current_month and r.dim_value == mover.dim_value),
            None,
        )
        prv_row = next(
            (r for r in slice_rows
             if r.campaign_month == mover.prior_month and r.dim_value == mover.dim_value),
            None,
        )
        if cur_row is None or prv_row is None:
            continue
        trm_delta = _delta_bps(cur_row.expected_rr_trm, prv_row.expected_rr_trm)
        xpm_delta = _delta_bps(cur_row.expected_rr_xpm, prv_row.expected_rr_xpm)
        rows.append(ModelCatchRow(
            dim=mover.dim,
            dim_value=mover.dim_value,
            period="MoM",
            current_month=mover.current_month,
            prior_month=mover.prior_month,
            actual_delta_bps=mover.delta_bps,
            trm_expected_delta_bps=trm_delta,
            xpm_expected_delta_bps=xpm_delta,
            trm_verdict=_verdict(mover.delta_bps, trm_delta),
            xpm_verdict=_verdict(mover.delta_bps, xpm_delta),
        ))

    trm_summary: dict[str, int] = {}
    xpm_summary: dict[str, int] = {}
    for r in rows:
        trm_summary[r.trm_verdict] = trm_summary.get(r.trm_verdict, 0) + 1
        xpm_summary[r.xpm_verdict] = xpm_summary.get(r.xpm_verdict, 0) + 1
    return ModelCatchAnalysis(rows=rows, trm_summary=trm_summary, xpm_summary=xpm_summary)


def _delta_bps(cur: float | None, prv: float | None) -> float | None:
    if cur is None or prv is None:
        return None
    return round((cur - prv) * 10_000, 2)


def _verdict(
    actual: float | None, expected: float | None,
) -> Literal["match", "partial", "miss", "n/a"]:
    if actual is None or expected is None:
        return "n/a"
    # Expected flat while actual moved materially → model didn't see it.
    if abs(expected) < _EXPECTED_FLAT_BPS:
        return "miss"
    # Wrong direction → model said the opposite.
    if (actual >= 0) != (expected >= 0):
        return "miss"
    # Same direction; check magnitude tolerance.
    ratio = abs(expected) / abs(actual) if abs(actual) > 0 else None
    if ratio is None:
        return "n/a"
    # Within (0.5, 1.5) of actual magnitude — model is in the right ballpark.
    if (1 - _MATCH_MAGNITUDE_TOL) <= ratio <= (1 + _MATCH_MAGNITUDE_TOL):
        return "match"
    return "partial"
