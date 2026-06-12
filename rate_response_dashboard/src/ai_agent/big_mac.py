"""Big Mac cohort drill-down.

The Big Mac is the company's primary "untouched baseline" cohort: prospects
that aren't being remailed, aren't being expanded, and sit at the model's
flagship tiers. Movement in Big Mac NRR is the cleanest signal of pure
response-rate change — confounds from remails / expansions / TM stacking
don't apply.

snapshot_builder already filtered the rollup mart with `big_mac_filter_expr`
(centralized so we can't drift between modules) and dropped pre-aggregated
slices into ReportFacts. This module just turns those slices into the
biggest-drop / biggest-gain narrative.
"""
from __future__ import annotations

from .facts import BigMacAnalysis, Movement, ReportFacts
from .mom_yoy import _make_movement


def compute(facts: ReportFacts, big_mac_cfg: dict) -> BigMacAnalysis:
    filter_summary = {k: v for k, v in (big_mac_cfg or {}).items() if k != "drill_dim"}
    drill_dim = (big_mac_cfg or {}).get("drill_dim")

    if not facts.big_mac_trend:
        return BigMacAnalysis(
            filter_summary=filter_summary,
            drill_dim=drill_dim,
            overall_trend=[],
            by_drill_trend=[],
            biggest_drop=None,
            biggest_gain=None,
            cohort_empty=True,
        )

    months = sorted({r.campaign_month for r in facts.big_mac_trend})
    if len(months) < 2 or not facts.big_mac_by_drill_trend:
        return BigMacAnalysis(
            filter_summary=filter_summary,
            drill_dim=drill_dim,
            overall_trend=list(facts.big_mac_trend),
            by_drill_trend=list(facts.big_mac_by_drill_trend),
            biggest_drop=None,
            biggest_gain=None,
            cohort_empty=False,
        )

    latest, prior = months[-1], months[-2]
    drill_rows = facts.big_mac_by_drill_trend
    cur = {r.dim_value: r for r in drill_rows if r.campaign_month == latest}
    prv = {r.dim_value: r for r in drill_rows if r.campaign_month == prior}

    moves: list[Movement] = []
    for v, c in cur.items():
        p = prv.get(v)
        if p is None or c.nrr is None or p.nrr is None:
            continue
        moves.append(_make_movement(
            f"big_mac/{drill_dim}", v, "nrr", "MoM",
            c.campaign_month, p.campaign_month, c.nrr, p.nrr,
        ))

    if not moves:
        return BigMacAnalysis(
            filter_summary=filter_summary,
            drill_dim=drill_dim,
            overall_trend=list(facts.big_mac_trend),
            by_drill_trend=list(facts.big_mac_by_drill_trend),
            biggest_drop=None,
            biggest_gain=None,
            cohort_empty=False,
        )

    sorted_by_signed = sorted(moves, key=lambda m: m.delta_bps or 0.0)
    biggest_drop = sorted_by_signed[0] if (sorted_by_signed[0].delta_bps or 0) < 0 else None
    biggest_gain = sorted_by_signed[-1] if (sorted_by_signed[-1].delta_bps or 0) > 0 else None

    return BigMacAnalysis(
        filter_summary=filter_summary,
        drill_dim=drill_dim,
        overall_trend=list(facts.big_mac_trend),
        by_drill_trend=list(facts.big_mac_by_drill_trend),
        biggest_drop=biggest_drop,
        biggest_gain=biggest_gain,
        cohort_empty=False,
    )
