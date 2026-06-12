"""Population mix analysis — how each dim's volume share shifted MoM.

Pure transformation on ReportFacts. We do this BEFORE attribution because
mix shifts are the most common driver of "NRR went down" — if $0/$0
expanded its share from 20% → 30% and $0/$0 has the lowest NRR, the
overall NRR drop may be 100% mix and 0% within-segment rate change.

Shares are computed as `volume / sum(volume)` within (dim, campaign_month).
`delta_share_pp` is reported in percentage points (not bps; bps for shares
is confusing) so a +1.5 means the share grew by 1.5 percentage points.
"""
from __future__ import annotations

from .facts import Direction, MixAnalysis, MixShiftRow, ReportFacts


# Anything below 0.1 pp is reported "flat" — noise floor at typical mail volumes.
_FLAT_PP = 0.1
TOP_SHIFTS_K = 8


def compute(facts: ReportFacts, top_k: int = TOP_SHIFTS_K) -> MixAnalysis:
    if not facts.months_in_scope or len(facts.months_in_scope) < 2:
        return MixAnalysis(
            latest_month=facts.latest_month or "",
            prior_month=None,
        )
    latest = facts.latest_month
    prior = facts.months_in_scope[facts.months_in_scope.index(latest) - 1]

    by_dim: dict[str, list[MixShiftRow]] = {}
    all_shifts: list[MixShiftRow] = []

    for dim, rows in facts.segment_trend.items():
        latest_rows = [r for r in rows if r.campaign_month == latest]
        prior_rows = [r for r in rows if r.campaign_month == prior]
        tot_latest = sum((r.volume or 0.0) for r in latest_rows)
        tot_prior = sum((r.volume or 0.0) for r in prior_rows)
        if tot_latest <= 0 or tot_prior <= 0:
            continue
        latest_by_v = {r.dim_value: r for r in latest_rows}
        prior_by_v = {r.dim_value: r for r in prior_rows}
        shifts: list[MixShiftRow] = []
        for v in sorted(set(latest_by_v) | set(prior_by_v),
                        key=lambda x: (x is None, x)):
            cur = latest_by_v.get(v)
            prv = prior_by_v.get(v)
            cur_share = (cur.volume / tot_latest) if cur and cur.volume else None
            prv_share = (prv.volume / tot_prior) if prv and prv.volume else None
            delta_pp = None
            direction: Direction = "flat"
            if cur_share is not None and prv_share is not None:
                delta_pp = (cur_share - prv_share) * 100.0
                if delta_pp > _FLAT_PP:
                    direction = "up"
                elif delta_pp < -_FLAT_PP:
                    direction = "down"
            shifts.append(MixShiftRow(
                dim=dim, dim_value=v,
                current_month=latest, prior_month=prior,
                current_share=cur_share, prior_share=prv_share,
                delta_share_pp=delta_pp, direction=direction,
            ))
        # Sort within dim by absolute shift desc for readability.
        shifts.sort(key=lambda s: abs(s.delta_share_pp or 0), reverse=True)
        by_dim[dim] = shifts
        all_shifts.extend(shifts)

    all_shifts.sort(key=lambda s: abs(s.delta_share_pp or 0), reverse=True)
    return MixAnalysis(
        latest_month=latest,
        prior_month=prior,
        by_dim=by_dim,
        top_shifts=all_shifts[:top_k],
    )
