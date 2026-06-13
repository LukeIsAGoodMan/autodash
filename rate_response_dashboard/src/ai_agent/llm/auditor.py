"""Independent audit pass over LLM-generated commentary.

The writer (`llm/writer.py`) is the *author*. This module is the *reviewer*:
same LLM endpoint (so it works on Gemini Enterprise without provisioning a
second vendor), fresh system prompt that frames the model as a skeptical
QA reader who did NOT write the commentary.

Each section is audited independently using the same facts payload the
original author saw, plus the slots the author actually wrote. Findings
are keyed by **section letter** ('A', 'B', 'C', 'D', 'E', 'F') so the
template can show one banner per top-level section regardless of how many
sub-builders (A, B-{dim}, B-summary, ...) contributed.

Never raises — a failed audit call falls back to "audit unavailable" info
issue so the report still renders.
"""
from __future__ import annotations

import json
import logging
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
