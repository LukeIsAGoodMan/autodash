"""Slice-trend bundle.

Thin wrapper that picks `chart_lookback_months` from segment_trend so the
renderer/chart_builder doesn't have to do that bookkeeping. No new analysis
happens here — just selection.
"""
from __future__ import annotations

from .facts import ReportFacts, SliceTrendBundle


def compute(facts: ReportFacts, lookback_months: int) -> SliceTrendBundle:
    if not facts.months_in_scope:
        return SliceTrendBundle(months=[])
    months = facts.months_in_scope[-lookback_months:] if lookback_months > 0 else facts.months_in_scope
    keep = set(months)
    by_dim: dict[str, list] = {}
    for dim, rows in facts.segment_trend.items():
        by_dim[dim] = [r for r in rows if r.campaign_month in keep]
    return SliceTrendBundle(months=months, by_dim=by_dim)
