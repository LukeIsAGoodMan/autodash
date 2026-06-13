"""Independent audit pass over LLM-generated commentary.

Two tiers, both running on the same LLM endpoint:

1. `audit_commentary` — one call per section. Catches WITHIN-section
   defects: bps/pp unit bugs, hallucinated numbers, missing caveats,
   causal language, internal contradictions, weak prioritization.

2. `audit_global` — one final call that reduces over per-section
   findings + slot headlines + a compressed view of top-level facts.
   Catches CROSS-section defects that no local auditor can see:
   numbers cited inconsistently between sections, narrative gaps where
   sections don't connect, and severity miscalibration when multiple
   per-section findings trace to the same root cause.

Each tier is gated by its own config flag (`audit_enabled` and
`global_audit_enabled`), defaults true. Neither tier ever raises — a
failed call becomes an `info` issue that the report still renders.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, Field

from ..facts import ReportPackage
from .client import LLMClient, LLMError
from .prompts import get_section_builders
from .schemas import CommentarySlot


log = logging.getLogger(__name__)


_AUDIT_SYSTEM = (
    "You are an INDEPENDENT QA reviewer for AI-generated direct-mail "
    "performance report commentary. You did NOT write the commentary you "
    "are reviewing — read it skeptically. Your job is to flag specific, "
    "actionable defects so an analyst can correct them before publication.\n"
    "\n"
    "Check for these defect categories:\n"
    "\n"
    "1. UNIT BUGS. bps is for RATE deltas (NRR, board rate, A/E ratios). "
    "pp (percentage points) is for SHARE deltas (volume share, mix share). "
    "A claim like '+300 bps' applied to a share value (e.g. share rose to "
    "32% from 29%) is a CRITICAL bug — the underlying delta is +3.0 pp.\n"
    "\n"
    "2. HALLUCINATED NUMBERS. Every number cited in commentary MUST appear "
    "in the facts payload or be a trivial derivation. If a number does not "
    "match anything in facts, flag it.\n"
    "\n"
    "3. MISSING CAVEATS. If the facts say maturity is 'partial' or "
    "'unknown' for the latest month, the body MUST prefix with "
    "'Preliminary:' and call the data 'still maturing'. If has_xpm is "
    "false, the commentary MUST explicitly say 'XPM unavailable'.\n"
    "\n"
    "4. CAUSAL LANGUAGE without justification. 'caused by', 'due to', "
    "'driven by', 'because of' are forbidden — there is no PAF event "
    "integration yet, so any causal claim is a violation.\n"
    "\n"
    "5. INTERNAL CONTRADICTIONS. Headline and body must agree. Numbers "
    "cited in material_movers should appear in the body. noise_flags "
    "should not be the lead of the body.\n"
    "\n"
    "6. MISSING PRIORITIZATION on ranking slots. slice_*, "
    "section_b_summary, section_d_summary, top_combo_movers, big_mac_drill "
    "are ranking slots — if material_movers is empty, the body MUST say "
    "'everything is within noise' explicitly. Otherwise the absent ranking "
    "is itself a defect.\n"
    "\n"
    "Be SPECIFIC. 'Mentions wrong number' is useless. 'headline says "
    "+25 bps MoM but facts.overall_mom[0].delta_bps shows +12.4 bps for "
    "nrr' is useful — cite the slot id and the conflicting field path.\n"
    "\n"
    "Severity guidance:\n"
    "  • error   = unit bug, hallucinated number, causal language, missing "
    "XPM/maturity caveat\n"
    "  • warning = weak prioritization, internal contradiction, omitted "
    "expected mover\n"
    "  • info    = stylistic nit, awkward phrasing, redundancy across slots\n"
    "\n"
    "If nothing is wrong, return an empty `issues` list. DO NOT manufacture "
    "findings to look thorough — manufactured findings are worse than a "
    "clean audit."
)


class AuditIssue(BaseModel):
    severity: Literal["error", "warning", "info"]
    issue: str = Field(
        ..., min_length=10, max_length=400,
        description=(
            "One sentence stating WHAT is wrong, citing specifics: slot id, "
            "the field path in facts that contradicts the commentary, the "
            "specific phrase that violates the rule."
        ),
    )
    affected_slot: str = Field(
        default="",
        description="slot_id this issue applies to. Empty if cross-slot.",
    )
    suggestion: str = Field(
        default="",
        description="Optional. One sentence on how to fix.",
    )


class AuditReport(BaseModel):
    section_id: str
    issues: list[AuditIssue] = Field(default_factory=list)


def _section_letter(section_id: str) -> str:
    """Map 'A', 'B-summary', 'B-annual_fee', 'C', ... → 'A', 'B', 'B', 'C', ...

    Audit findings group at the top-level section so the template can show
    one banner per visible section regardless of how many sub-builders
    contributed."""
    return section_id.split("-", 1)[0]


def _slots_to_audit(
    expected_ids: list[str], commentary: dict[str, CommentarySlot],
) -> list[dict]:
    """Pick the slots the writer actually produced for this section.

    Skips fallback slots (headline starts with 'Commentary unavailable')
    because auditing a fallback is wasted spend — the fallback itself is
    the audit verdict.
    """
    out = []
    for sid in expected_ids:
        slot = commentary.get(sid)
        if slot is None:
            continue
        if slot.headline.lower().startswith("commentary unavailable"):
            continue
        out.append(slot.model_dump(mode="json"))
    return out


def audit_commentary(
    pkg: ReportPackage,
    commentary: dict[str, CommentarySlot],
    client: LLMClient,
) -> dict[str, list[AuditIssue]]:
    """Run one audit LLM call per section. Returns section_letter → issues.

    Never raises — a failed audit call appends a single 'info' issue
    indicating the audit could not be performed, so the rest of the
    report still renders cleanly.
    """
    findings: dict[str, list[AuditIssue]] = {}
    for section_id, builder in get_section_builders(pkg):
        letter = _section_letter(section_id)
        try:
            _, original_user, expected_ids = builder(pkg, commentary)
        except Exception as e:
            log.exception("Audit: section %s builder error: %s", section_id, e)
            continue

        slots_payload = _slots_to_audit(expected_ids, commentary)
        if not slots_payload:
            continue                            # nothing to review for this section

        audit_user = (
            "ORIGINAL FACTS (what the writer was given — your ground truth):\n"
            f"{original_user}\n"
            "\n"
            "COMMENTARY UNDER REVIEW (what the writer produced):\n"
            f"{json.dumps(slots_payload, indent=2, default=str)}\n"
            "\n"
            "Audit. Return an AuditReport whose `issues` list contains one "
            "AuditIssue per defect you can specifically cite. Empty list "
            "is a valid (and preferred) answer when the commentary is "
            "clean."
        )

        try:
            report = client.generate_structured(
                system=_AUDIT_SYSTEM, user=audit_user, schema=AuditReport,
            )
        except LLMError as e:
            log.warning("Section %s audit failed: %s", section_id, e)
            findings.setdefault(letter, []).append(AuditIssue(
                severity="info",
                issue=f"Audit pass could not complete for sub-section {section_id}: {str(e)[:200]}",
                affected_slot="",
                suggestion="",
            ))
            continue
        except Exception as e:
            log.exception("Section %s audit unexpected error: %s", section_id, e)
            findings.setdefault(letter, []).append(AuditIssue(
                severity="info",
                issue=f"Audit pass crashed for sub-section {section_id}: {type(e).__name__}",
                affected_slot="",
                suggestion="",
            ))
            continue

        if report.issues:
            findings.setdefault(letter, []).extend(report.issues)

    return findings


# ============================================================================
# Global audit — final pass over the whole report
# ============================================================================


_GLOBAL_AUDIT_SYSTEM = (
    "You are the FINAL QA reviewer for a monthly direct-mail performance "
    "report. The per-section auditors have already flagged within-section "
    "defects. Your job is the layer above them: catch problems that only "
    "appear when looking at the WHOLE report.\n"
    "\n"
    "Check for exactly three categories:\n"
    "\n"
    "1. CROSS-SECTION NUMBER CONFLICTS. The same metric cited in two "
    "different slots must agree. If Section A's headline says NRR +25 bps "
    "MoM but Section F's calibration prose says NRR was flat, flag it. "
    "Cite BOTH slot ids and BOTH conflicting numbers in the issue text.\n"
    "\n"
    "2. NARRATIVE GAPS. Each section tells a local story; the report "
    "should tell a coherent one. If Section B identifies $95/$95 as the "
    "dominant mix driver but Section D's top combo movers never mention "
    "any $95/$95 cells, that is a gap — either the mix story is wrong or "
    "D missed a connection. If sections read disconnected — each making "
    "its own point with no shared theme — flag it as a narrative-gap "
    "warning.\n"
    "\n"
    "3. SEVERITY CALIBRATION across per-section findings. The per-section "
    "auditors made local calls. If several sections all flag the same "
    "root cause (e.g., 4 sections all report unit bugs that trace to one "
    "stale MoM input), say so explicitly so the analyst fixes the root "
    "once rather than the symptom 4 times. If a per-section finding "
    "is rated 'error' but is really stylistic when read alongside the "
    "rest of the report, note it as info.\n"
    "\n"
    "Severity guidance for YOUR findings:\n"
    "  • error   = cross-section number conflict, broken narrative thread\n"
    "  • warning = weaker disconnects, redundancy across sections, "
    "miscalibrated per-section severity\n"
    "  • info    = stylistic / observational; the report still ships\n"
    "\n"
    "Output rules:\n"
    "  - Be specific. Cite slot ids and the conflicting numbers / phrases, "
    "not vibes.\n"
    "  - Empty list is a valid AND PREFERRED answer if the report holds "
    "together. DO NOT manufacture findings.\n"
    "  - Cap total issues at 6. If you find more than 6, return the "
    "6 highest-priority ones — the analyst's attention budget is finite."
)


class GlobalAuditReport(BaseModel):
    """Report-level audit output. Same issue shape as section audit."""
    issues: list[AuditIssue] = Field(default_factory=list)


def _compress_slot(slot: CommentarySlot) -> dict:
    """Drop the full body — global auditor only needs headline + chips
    to assess narrative coherence. Cuts payload by ~10x."""
    return {
        "slot_id": slot.slot_id,
        "headline": slot.headline,
        "material_movers": list(getattr(slot, "material_movers", []) or []),
        "noise_flags": list(getattr(slot, "noise_flags", []) or []),
    }


def _headline_facts(pkg: ReportPackage) -> dict:
    """The smallest fact view sufficient for cross-section reasoning.

    Deliberately *not* every fact — global auditor reasons over the
    commentary, not the raw data. We just need enough to verify that
    the commentary's claims have a plausible root in the facts.
    """
    mm = pkg.mom_yoy
    return {
        "report_month": pkg.facts.latest_month,
        "overall_mom": [asdict(m) for m in (mm.overall_mom or [])][:4],
        "overall_yoy": [asdict(m) for m in (mm.overall_yoy or [])][:4],
        "top_mix_shifts": [asdict(s) for s in (pkg.mix.top_shifts or [])][:5],
        "big_mac_summary": {
            "cohort_empty": pkg.big_mac.cohort_empty,
            "biggest_gain": asdict(pkg.big_mac.biggest_gain) if pkg.big_mac.biggest_gain else None,
            "biggest_drop": asdict(pkg.big_mac.biggest_drop) if pkg.big_mac.biggest_drop else None,
        },
        "top_combo_gainers": [asdict(c) for c in (pkg.combinations.top_gainers or [])][:3],
        "top_combo_losers":  [asdict(c) for c in (pkg.combinations.top_losers or [])][:3],
        "model_catch_summary": {
            "trm": pkg.model_catch.trm_summary,
            "xpm": pkg.model_catch.xpm_summary,
        },
    }


def audit_global(
    pkg: ReportPackage,
    commentary: dict[str, CommentarySlot],
    section_findings: dict[str, list[AuditIssue]],
    client: LLMClient,
) -> list[AuditIssue]:
    """Run ONE LLM call that reduces over the whole report.

    Input is intentionally compressed (~5-10K tokens) so the auditor's
    attention stays sharp:
      - headline + chips for every populated slot (no bodies)
      - per-section audit findings (already short)
      - a top-N fact summary (overall mom/yoy, top mix shifts, top combo
        movers, model-catch counts) — just enough to cross-check claims

    Never raises — failure becomes a single info issue.
    """
    # Skip slots that are themselves fallbacks; their content is the
    # audit verdict, no point feeding them to the auditor.
    slot_view = [
        _compress_slot(slot)
        for slot in commentary.values()
        if not slot.headline.lower().startswith("commentary unavailable")
    ]
    if not slot_view:
        return []

    # Per-section findings → JSON-safe; downgrade Pydantic models to dicts.
    findings_view = {
        letter: [iss.model_dump(mode="json") for iss in issues]
        for letter, issues in section_findings.items()
    }

    payload = {
        "instruction": (
            "Audit the whole report. Look for cross-section number "
            "conflicts, narrative gaps, and severity miscalibration. "
            "Return only specific, citation-backed findings."
        ),
        "headline_facts": _headline_facts(pkg),
        "section_outputs": slot_view,
        "section_local_audit_findings": findings_view,
    }
    user = (
        "Below is a compressed view of the whole report. Audit it.\n\n"
        f"{json.dumps(payload, indent=2, default=str)}"
    )

    try:
        report = client.generate_structured(
            system=_GLOBAL_AUDIT_SYSTEM, user=user, schema=GlobalAuditReport,
        )
    except LLMError as e:
        log.warning("Global audit failed: %s", e)
        return [AuditIssue(
            severity="info",
            issue=f"Report-level audit pass could not complete: {str(e)[:200]}",
            affected_slot="",
            suggestion="",
        )]
    except Exception as e:
        log.exception("Global audit unexpected error: %s", e)
        return [AuditIssue(
            severity="info",
            issue=f"Report-level audit pass crashed: {type(e).__name__}",
            affected_slot="",
            suggestion="",
        )]

    # Cap at 6 per the prompt rule — defensive in case the model ignores.
    return list(report.issues)[:6]
