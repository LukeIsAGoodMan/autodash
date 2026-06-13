"""Section-by-section commentary prompts.

Each builder returns (system, user, expected_slot_ids).
- `system` carries the role + constraints (same across sections for tone).
- `user` carries the section-specific facts as JSON + a few-shot example.
- `expected_slot_ids` is what we ask the LLM to populate.

Section B is split into per-dim sub-calls because the combined payload was
17K tokens (most of the report). One small call per dim keeps each
context focused and parallelizable.

All numeric values in the user payload are rounded to 4 decimal places —
the LLM does not need 15-digit precision and rounding cuts token cost.
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Callable

from ..facts import ReportPackage


_BASE_SYSTEM = (
    "You are a senior direct-mail performance analyst preparing a monthly "
    "rate-response report. Write in English. Output STRUCTURED commentary "
    "that the report renderer will embed under the relevant chart or table.\n"
    "\n"
    "Your job is NOT to restate every number in the payload — the chart "
    "already shows the numbers. Your job is to make a MATERIALITY CALL: "
    "lead with the mover that matters, and explicitly demote noise.\n"
    "\n"
    "Hard constraints — apply to every slot:\n"
    "1. UNITS. Use bps for RATE deltas (NRR, board rate, A/E ratios). Use "
    "pp (percentage points) for SHARE deltas (volume share, mix share). "
    "NEVER mix them. A +4.5 pp share change is +4.5 pp, not +450 bps. "
    "Format rates with two decimal places (1.37%).\n"
    "2. NO causal language ('caused', 'due to', 'because of', 'driven by'). "
    "Use 'coincides with', 'moved alongside', 'concurrent with' instead. "
    "Scan your body for these forbidden words before responding.\n"
    "3. PARTIAL MONTHS. If a month's maturity is 'partial' or 'unknown', "
    "PREFIX the relevant body with 'Preliminary:' and call the data 'still "
    "maturing'.\n"
    "4. XPM. If XPM is marked unavailable, EXPLICITLY say 'XPM unavailable "
    "for {month}'. Do not infer XPM values.\n"
    "5. PRIORITIZE — this is the most important rule. Lead the body with "
    "the 1-2 most material movers. Populate `material_movers` (up to 4 "
    "short phrases, ordered most-to-least material) with the movers to "
    "focus on, and `noise_flags` (up to 4) with movers that look big but "
    "should be DEMOTED. Noise criteria: (a) sample below the configured "
    "min-volume filter, (b) latest month partial-maturity, (c) single-month "
    "spike that reverses a multi-month trend, (d) magnitude within ±1 pp "
    "for share deltas or ±5 bps for rate deltas at material volumes. If "
    "everything is noise, leave `material_movers` empty and say so in the "
    "body.\n"
    "6. JUDGMENT vs RECOMMENDATION. You MAY say 'X is material, Y is "
    "noise' — that is analyst judgment and is required. You MAY NOT "
    "predict future months ('next month will...') or suggest operational "
    "actions ('we should mail more...').\n"
    "7. Headline = ONE sentence, 6-15 words. Body = 40-120 words. Stay "
    "within bounds.\n"
    "8. Populate every slot in `slots_to_populate`. slot_id MUST match."
)


# ============================================================================
# Few-shot examples — one per slot pattern, matching demo-PPT style.
# Examples deliberately AVOID causal claims since Stage 2 has no PAF context.
# ============================================================================

EXAMPLES = {
    "overall_combo": {
        "slot_id": "overall_combo",
        "headline": "Overall NRR closed at 1.37%, up 25 bps over the six-month window.",
        "body": (
            "NRR moved from 1.12% in the earliest in-scope month to 1.37% at "
            "latest, a +25 bps rise. Board rate followed a similar shape, "
            "ending at 0.80% (+5 bps MoM). Mail volume stayed within an "
            "18.1M-21.0M band, so the rate movement is not a pure mix "
            "artifact. Preliminary: latest month is still maturing."
        ),
        "material_movers": [],
        "noise_flags": [],
    },
    "section_a_summary": {
        "slot_id": "section_a_summary",
        "headline": "Latest NRR 1.37% lands 25 bps above the lookback floor; A/E TRM steady at 0.85.",
        "body": (
            "Overall NRR finished at 1.37% in the latest month versus 1.12% "
            "six months earlier, a +25 bps rise. A/E TRM held in a narrow "
            "0.84-0.85 band over the same window, indicating consistent "
            "model overshoot rather than drift. XPM A/E enters the picture "
            "from 2026-03 onward at 0.98."
        ),
        "material_movers": [],
        "noise_flags": [],
    },
    "section_b_summary": {
        "slot_id": "section_b_summary",
        "headline": "annual_fee dominates the mix story; rm_flag and scorecard dims are noise.",
        "body": (
            "Synthesizing the per-dim findings: annual_fee is the dominant "
            "dim — $95/$95 leads with +4.5 pp share AND a paired +8 bps "
            "NRR. vs_band is second-ranked with one material mover (530-549, "
            "-4.3 pp share with a rate move). times_mailed shows a share "
            "shift but its per-dim noise_flags dominate — flat per-segment "
            "NRR means it is a population effect, not a rate signal. "
            "rm_flag and scorecard dims contributed no material movers."
        ),
        "material_movers": [
            "annual_fee dim: $95/$95 mix + rate",
            "vs_band dim: 530-549 mix + rate",
        ],
        "noise_flags": [
            "times_mailed dim: share moved but per-segment NRR did not",
            "rm_flag dim: no material movers",
            "scorecard dim: no material movers",
        ],
    },
    "slice_dim": {     # template for any per-dim slice
        "slot_id": "slice_annual_fee",
        "headline": "$95/$95 is the material annual_fee mover (+4.5 pp share, +8 bps NRR).",
        "body": (
            "The material annual_fee mover is $95/$95: share rose to 30.8% "
            "(+4.5 pp MoM) and per-product NRR ticked to 1.04% (+8 bps). "
            "$75/$99 share contracted to 50.1% (-3.7 pp) and its NRR at "
            "1.49% (+12 bps) moved alongside — a mix story more than a "
            "rate story. $0/$0 and $75/$75 share moves sit within the "
            "±1 pp historical band — treat as noise."
        ),
        "material_movers": [
            "$95/$95 share +4.5 pp with NRR +8 bps",
            "$75/$99 share -3.7 pp",
        ],
        "noise_flags": [
            "$0/$0 share move within ±1 pp",
            "$75/$75 share move within ±1 pp",
        ],
    },
    "section_c_summary": {
        "slot_id": "section_c_summary",
        "headline": "Big Mac cohort NRR closed at 1.55% (+21 bps MoM); vs_band 550-600 the sole material mover.",
        "body": (
            "The Big Mac cohort (untouched baseline) closed at 1.55% NRR, "
            "+21 bps MoM. Within the cohort the only material mover is "
            "vs_band 550-600 (+21 bps to 1.29%), which also accounts for "
            "nearly all cohort volume. The cohort moving alongside overall "
            "is consistent with a rate movement rather than a mix or "
            "test-cell artifact."
        ),
        "material_movers": ["vs_band 550-600 +21 bps"],
        "noise_flags": [],
    },
    "big_mac_overall": {
        "slot_id": "big_mac_overall",
        "headline": "Big Mac cohort NRR ended at 1.55%, up 21 bps over prior month.",
        "body": (
            "Cohort-level NRR rose from 1.34% to 1.55% MoM (+21 bps). "
            "Cohort volume held near 1.0k mailpieces. Preliminary: latest "
            "month still maturing — cohort size is small so single-month "
            "moves may not be representative."
        ),
        "material_movers": [],
        "noise_flags": ["small cohort: ~1k mailpieces total"],
    },
    "big_mac_drill": {
        "slot_id": "big_mac_drill",
        "headline": "Within Big Mac, vs_band 550-600 is the only material mover (+21 bps).",
        "body": (
            "Per-vs_band drill within the cohort shows 550-600 as the sole "
            "material mover (+21 bps to 1.29%) — it also accounts for "
            "nearly all cohort volume. Smaller bands (530-549, 701-730) "
            "had thin cohort volumes (<10 mailpieces) and were small-cell "
            "suppressed for rate reporting; treat any apparent rate move "
            "there as noise."
        ),
        "material_movers": ["vs_band 550-600 +21 bps to 1.29%"],
        "noise_flags": [
            "vs_band 530-549 thin cohort (<10 mailpieces)",
            "vs_band 701-730 thin cohort (<10 mailpieces)",
        ],
    },
    "section_d_summary": {
        "slot_id": "section_d_summary",
        "headline": "Gainers cluster on vs_band 701-730 x mid-TM; $0/$0 x TM 10 the standout drop.",
        "body": (
            "The standout gainers all share vs_band=701-730 paired with "
            "moderate times_mailed (6, 9, 10), each +34 to +46 bps MoM. "
            "The standout loser is $0/$0 x times_mailed=10 at -27 bps. "
            "Both sides passed the 5,000 mailpiece volume filter, so this "
            "is not small-cell noise. vs_band 530-549 movers (-22 bps) "
            "sit at the filter boundary and are demoted."
        ),
        "material_movers": [
            "vs_band 701-730 x TM 6/9/10: +34 to +46 bps",
            "$0/$0 x TM 10: -27 bps",
        ],
        "noise_flags": ["vs_band 530-549 cells near min-volume filter boundary"],
    },
    "top_combo_movers": {
        "slot_id": "top_combo_movers",
        "headline": "Material movers concentrate on vs_band x times_mailed; product/Prospect_type pairs quiet.",
        "body": (
            "The material gainers and losers both cluster on the vs_band "
            "x times_mailed_12mo_cnt pair, indicating the MoM movement "
            "lives in specific risk-band x mail-frequency cells rather "
            "than across the file. No product or Prospect_type pair "
            "reached the material threshold this month."
        ),
        "material_movers": [
            "vs_band x times_mailed cluster: gainers and losers both here",
        ],
        "noise_flags": [
            "no material product-pair movers this month",
            "no material Prospect_type pair movers this month",
        ],
    },
    "section_e_summary": {
        "slot_id": "section_e_summary",
        "headline": "TRM and XPM each matched 5/5 material movers; no model miss this month.",
        "body": (
            "Across the 5 material MoM movers, expected_rr_trm moved in "
            "the same direction with magnitudes within 50% of actual — "
            "all rated 'match'. XPM matched the same 5 movers (data "
            "available from 2026-03 onward). No segment-level rate change "
            "in this report's mover set went uncaught by either model."
        ),
        "material_movers": ["TRM caught 5/5", "XPM caught 5/5 (post 2026-03)"],
        "noise_flags": [],
    },
    "section_f_summary": {
        "slot_id": "section_f_summary",
        "headline": "TRM A/E stable at 0.85 — overshoot, not drift; XPM A/E lands closer to 1.0 at 0.98.",
        "body": (
            "TRM A/E sat in a narrow 0.85-0.85 band across the full "
            "window — consistent overshoot, not drift. XPM A/E became "
            "observable from 2026-03 onward (XPM unavailable for prior "
            "months) and tracks 0.98, materially closer to the 1.0 ideal "
            "than TRM."
        ),
        "material_movers": [
            "XPM A/E ~0.98 vs TRM ~0.85: XPM materially better-calibrated",
        ],
        "noise_flags": [],
    },
    "calibration": {
        "slot_id": "calibration",
        "headline": "TRM A/E pinned near 0.85 throughout; XPM appears from 2026-03 at 0.98.",
        "body": (
            "TRM A/E held a 0.85 baseline across all 15 months in scope. "
            "XPM A/E first becomes computable in 2026-03 at 0.98 and "
            "remains in the 0.98-0.99 band thereafter. The TRM gap to "
            "1.0 is stable, not widening — consistent calibration "
            "overshoot rather than incremental drift."
        ),
        "material_movers": ["TRM gap to 1.0 stable (not drifting)"],
        "noise_flags": [],
    },
}


# ============================================================================
# Helpers
# ============================================================================

def _round_floats(obj, ndigits: int = 4):
    """Recursively round floats to keep prompt payload compact and reduce
    LLM transcription errors. Rates printed at 4dp give 0.01 bp resolution,
    which is well below the report's noise floor."""
    if isinstance(obj, float):
        try:
            return round(obj, ndigits)
        except (TypeError, ValueError):
            return obj
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def _to_jsonable(obj):
    """Convert dataclasses / lists / dicts to JSON-safe types, then round."""
    if is_dataclass(obj):
        return _to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def _format_user_prompt(payload: dict, example_slot_ids: list[str]) -> str:
    """Assemble the user prompt: example-of-good-output + the facts payload."""
    examples = [EXAMPLES[sid] for sid in example_slot_ids if sid in EXAMPLES]
    return (
        "Below is one or more example slot outputs in the exact style and "
        "schema we want:\n"
        f"{json.dumps(examples, indent=2)}\n"
        "\n"
        "Now produce commentary for the following facts. Use the same style, "
        "same numeric formatting, and same conservative tone as the examples.\n"
        f"{json.dumps(_round_floats(payload), indent=2, default=str)}"
    )


# ============================================================================
# Section builders
# ============================================================================


def section_a(pkg: ReportPackage, prior_slots: dict | None = None) -> tuple[str, str, list[str]]:
    latest_kpi = next(
        (r for r in pkg.facts.overall_trend if r.campaign_month == pkg.facts.latest_month),
        None,
    )
    payload = {
        "section_id": "A",
        "topic": "Headline KPIs and overall trend",
        "latest_month": pkg.facts.latest_month,
        "maturity": asdict(pkg.facts.maturity[pkg.facts.latest_month])
            if pkg.facts.latest_month in pkg.facts.maturity else None,
        "kpi_latest_month": asdict(latest_kpi) if latest_kpi else None,
        "overall_trend_last_6": [asdict(r) for r in pkg.facts.overall_trend[-6:]],
        "overall_mom": [asdict(m) for m in pkg.mom_yoy.overall_mom],
        "overall_yoy": [asdict(m) for m in pkg.mom_yoy.overall_yoy],
        "slots_to_populate": ["section_a_summary", "overall_combo"],
    }
    return (_BASE_SYSTEM,
            _format_user_prompt(payload, ["section_a_summary", "overall_combo"]),
            payload["slots_to_populate"])


def section_b_summary(pkg: ReportPackage, prior_slots: dict | None = None) -> tuple[str, str, list[str]]:
    """Cross-dim summary. Runs AFTER per-dim builders so the prompt can
    feed in each dim's headline + materiality call, letting the model
    synthesize across dims rather than re-deriving rankings from raw mix
    shifts."""
    # Pull each per-dim slice_{dim} slot the writer already produced so the
    # synthesis step can lean on (and cross-check) the per-dim conclusions.
    per_dim_findings: dict = {}
    if prior_slots:
        for dim in pkg.slice_trends.by_dim.keys():
            slot_id = f"slice_{dim}"
            slot = prior_slots.get(slot_id)
            if slot is None:
                continue
            per_dim_findings[dim] = {
                "headline": slot.headline,
                "material_movers": list(getattr(slot, "material_movers", []) or []),
                "noise_flags": list(getattr(slot, "noise_flags", []) or []),
            }
    payload = {
        "section_id": "B-summary",
        "topic": "Cross-dimensional mix-shift summary",
        "latest_month": pkg.facts.latest_month,
        "mix_top_shifts": [asdict(s) for s in pkg.mix.top_shifts[:10]],
        "per_dim_findings": per_dim_findings,
        "instruction": (
            "Use per_dim_findings to synthesize across dims. Lead with the "
            "single most material dim. Demote any dim whose per-dim "
            "noise_flags dominate its material_movers — that means the dim "
            "moved but the rate did not follow. Do not just list the mix "
            "shifts; the per-dim charts below already do that."
        ),
        "slots_to_populate": ["section_b_summary"],
    }
    return (_BASE_SYSTEM,
            _format_user_prompt(payload, ["section_b_summary"]),
            payload["slots_to_populate"])


def make_section_b_dim(dim: str) -> Callable[..., tuple[str, str, list[str]]]:
    """Return a builder for one slice dimension. Each builder produces one
    small LLM call covering ONLY that dim's trend + mix shifts."""
    slot_id = f"slice_{dim}"

    def _builder(pkg: ReportPackage, prior_slots: dict | None = None) -> tuple[str, str, list[str]]:
        rows = pkg.slice_trends.by_dim.get(dim, [])
        keep_months = set(pkg.facts.months_in_scope[-3:])    # trim to 3 months
        rows_recent = [r for r in rows if r.campaign_month in keep_months]
        # Per-dim mix shifts only.
        dim_shifts = pkg.mix.by_dim.get(dim, [])[:6]
        payload = {
            "section_id": f"B-{dim}",
            "topic": f"Volume mix and NRR trend for dimension '{dim}'",
            "latest_month": pkg.facts.latest_month,
            "dim_shifts_mom": [asdict(s) for s in dim_shifts],
            "trend_last_3_months": [asdict(r) for r in rows_recent],
            "slots_to_populate": [slot_id],
        }
        return (_BASE_SYSTEM,
                _format_user_prompt(payload, ["slice_dim"]),
                payload["slots_to_populate"])
    _builder.__name__ = f"section_b_{dim}"
    return _builder


def section_c(pkg: ReportPackage, prior_slots: dict | None = None) -> tuple[str, str, list[str]]:
    slot_ids = ["section_c_summary", "big_mac_overall", "big_mac_drill"]
    payload = {
        "section_id": "C",
        "topic": "Big Mac cohort drill-down (untouched baseline)",
        "latest_month": pkg.facts.latest_month,
        "big_mac_filter": pkg.big_mac.filter_summary,
        "drill_dim": pkg.big_mac.drill_dim,
        "cohort_empty": pkg.big_mac.cohort_empty,
        "overall_trend_last_6": [asdict(r) for r in pkg.big_mac.overall_trend[-6:]],
        "by_drill_latest": [
            asdict(r) for r in pkg.big_mac.by_drill_trend
            if r.campaign_month == pkg.facts.latest_month
        ],
        "biggest_drop": asdict(pkg.big_mac.biggest_drop) if pkg.big_mac.biggest_drop else None,
        "biggest_gain": asdict(pkg.big_mac.biggest_gain) if pkg.big_mac.biggest_gain else None,
        "slots_to_populate": slot_ids,
    }
    return (_BASE_SYSTEM,
            _format_user_prompt(payload, slot_ids),
            slot_ids)


def section_d(pkg: ReportPackage, prior_slots: dict | None = None) -> tuple[str, str, list[str]]:
    slot_ids = ["section_d_summary", "top_combo_movers"]
    payload = {
        "section_id": "D",
        "topic": "Top 2-way combination movers (MoM NRR)",
        "latest_month": pkg.combinations.latest_month,
        "prior_month": pkg.combinations.prior_month,
        "min_volume_filter": pkg.combinations.min_volume,
        "pairs_evaluated": pkg.combinations.pairs_evaluated,
        "top_gainers": [asdict(m) for m in pkg.combinations.top_gainers],
        "top_losers": [asdict(m) for m in pkg.combinations.top_losers],
        "slots_to_populate": slot_ids,
    }
    return (_BASE_SYSTEM,
            _format_user_prompt(payload, slot_ids),
            slot_ids)


def section_e(pkg: ReportPackage, prior_slots: dict | None = None) -> tuple[str, str, list[str]]:
    slot_ids = ["section_e_summary"]
    payload = {
        "section_id": "E",
        "topic": "Did the TRM/XPM expected NRR catch the observed movers?",
        "latest_month": pkg.facts.latest_month,
        "trm_summary": pkg.model_catch.trm_summary,
        "xpm_summary": pkg.model_catch.xpm_summary,
        "rows": [asdict(r) for r in pkg.model_catch.rows],
        "slots_to_populate": slot_ids,
    }
    return (_BASE_SYSTEM,
            _format_user_prompt(payload, slot_ids),
            slot_ids)


def section_f(pkg: ReportPackage, prior_slots: dict | None = None) -> tuple[str, str, list[str]]:
    slot_ids = ["section_f_summary", "calibration"]
    payload = {
        "section_id": "F",
        "topic": "TRM vs XPM model choice — calibration and rank order",
        "latest_month": pkg.model.latest_month,
        "headline_rank_order": [asdict(s) for s in pkg.model.headline],
        "calibration_trend": [asdict(c) for c in pkg.model.calibration_trend],
        "slots_to_populate": slot_ids,
        "note_for_llm": (
            "Comment on whether TRM A/E drifts away from 1.0 over time. "
            "Explicitly note the XPM availability gap (months where "
            "available=false). Compare XPM A/E to TRM A/E only in months "
            "where both are available."
        ),
    }
    return (_BASE_SYSTEM,
            _format_user_prompt(payload, slot_ids),
            slot_ids)


# ============================================================================
# Section dispatch table. Note Section B is expanded into 1 summary + N per-dim
# builders at runtime so the iteration order is stable but the dim count
# comes from the live ReportPackage.
# ============================================================================

def get_section_builders(pkg: ReportPackage) -> list[tuple[str, Callable]]:
    """Build the (section_id, builder) list for one report instance.

    Per-dim builders for Section B are generated on the fly so adding/removing
    a slice dim in config.yaml doesn't require touching this module.

    Dispatch order matters: per-dim B-{dim} builders run BEFORE B-summary so
    the summary call can see each dim's headline + materiality call via
    `prior_slots` and synthesize across dims rather than re-deriving the
    ranking from raw mix shifts.
    """
    builders: list[tuple[str, Callable]] = [("A", section_a)]
    for dim in pkg.slice_trends.by_dim.keys():
        builders.append((f"B-{dim}", make_section_b_dim(dim)))
    builders.extend([
        ("B-summary", section_b_summary),
        ("C", section_c),
        ("D", section_d),
        ("E", section_e),
        ("F", section_f),
    ])
    return builders
