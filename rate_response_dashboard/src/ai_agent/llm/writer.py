"""Section-level commentary writer.

Orchestrates one LLM call per section, validates the response against the
SectionCommentary schema, and returns a flat slot_id → CommentarySlot
mapping that the renderer can look up by chart name or section id.

Failure modes — all caught, never raised:

  - API key missing / client construction fails  → see `populate_all_fallback`
  - Network / timeout / auth error in one section → per-section fallback
  - LLM returns empty slots list                  → per-section fallback
  - Unknown exception inside builder or call      → per-section fallback

In every failure the affected slot_ids get a CommentarySlot whose headline
and body name the reason, so the rendered report tells the analyst exactly
what is missing rather than leaving a silent gap.
"""
from __future__ import annotations

import logging

from ..facts import ReportPackage
from .client import LLMClient, LLMError
from .prompts import get_section_builders
from .schemas import CommentarySlot, SectionCommentary


log = logging.getLogger(__name__)


_FALLBACK_BODY_TEMPLATE = (
    "AI commentary could not be generated for this slot.\n\n"
    "Reason: {reason}\n\n"
    "Common causes:\n"
    "  • API key not set — export the env var named in "
    "ai_agent.llm.api_key_env (default OPENAI_API_KEY) before launching.\n"
    "  • Network / timeout — the LLM did not respond within "
    "ai_agent.llm.timeout_seconds.\n"
    "  • Provider returned an empty or invalid response.\n\n"
    "The rest of the report is deterministic and remains valid."
)


def _fallback_slot(slot_id: str, reason: str) -> CommentarySlot:
    """Build a CommentarySlot that surfaces *why* the LLM step failed.

    Lengths are clipped to fit the schema bounds (headline ≤200, body
    ≤1200) so this can never itself raise a validation error.
    """
    reason = reason.strip() or "unknown error"
    short_reason = reason[:140]
    headline = f"Commentary unavailable — {short_reason}"[:200]
    body = _FALLBACK_BODY_TEMPLATE.format(reason=reason[:600])[:1200]
    return CommentarySlot(
        slot_id=slot_id,
        headline=headline,
        body=body,
        material_movers=[],
        # Surface the failure as a noise chip so the reader spots it at a glance.
        noise_flags=[f"LLM unavailable: {short_reason[:80]}"],
    )


def _populate_fallbacks(
    target: dict[str, CommentarySlot], slot_ids: list[str], reason: str,
) -> None:
    """Fill `target` with fallback slots for every id not already present."""
    for sid in slot_ids:
        target.setdefault(sid, _fallback_slot(sid, reason))


def populate_all_fallback(
    pkg: ReportPackage, reason: str,
) -> dict[str, CommentarySlot]:
    """Build a commentary dict where every expected slot is a fallback.

    Called by the orchestrator when the LLM client cannot even be
    constructed (e.g. missing API key) — no LLM call ever happens, but
    the report still names the reason for every slot.
    """
    all_slots: dict[str, CommentarySlot] = {}
    for section_id, builder in get_section_builders(pkg):
        try:
            _, _, expected_ids = builder(pkg, all_slots)
        except Exception as e:                          # builder itself broke
            log.exception("Section %s builder error during fallback: %s",
                          section_id, e)
            continue
        _populate_fallbacks(all_slots, expected_ids, reason)
    return all_slots


def write_commentary(
    pkg: ReportPackage, client: LLMClient,
) -> dict[str, CommentarySlot]:
    """Run all configured sections through the LLM. Returns slot_id → slot.

    Never raises — every failure becomes a fallback slot that names the
    reason in its headline and body.
    """
    all_slots: dict[str, CommentarySlot] = {}
    for section_id, builder in get_section_builders(pkg):
        try:
            # Pass slots accumulated so far — lets B-summary (which runs AFTER
            # per-dim B-{dim} calls) synthesize across the per-dim findings
            # instead of re-deriving the ranking from raw mix shifts.
            system, user, expected_ids = builder(pkg, all_slots)
        except Exception as e:
            log.exception("Section %s builder error: %s", section_id, e)
            # Builder broke before we even know the slot ids — skip; nothing
            # in `pkg.commentary` for these slots, renderer shows placeholder.
            continue

        try:
            result = client.generate_structured(
                system=system, user=user, schema=SectionCommentary,
            )
        except LLMError as e:
            log.warning("Section %s LLM call failed: %s", section_id, e)
            _populate_fallbacks(all_slots, expected_ids, str(e))
            continue
        except Exception as e:                          # belt + suspenders
            log.exception("Section %s unexpected error: %s", section_id, e)
            _populate_fallbacks(
                all_slots, expected_ids,
                f"unexpected {type(e).__name__}: {e}",
            )
            continue

        if not result.slots:
            _populate_fallbacks(
                all_slots, expected_ids,
                "LLM returned an empty slots list",
            )
            continue

        # Merge slots returned by the model. If the model returned a slot
        # not in expected_ids we still take it (the renderer ignores
        # unknown slot ids), but log it so we can spot prompt drift.
        for slot in result.slots:
            if slot.slot_id in all_slots:
                log.warning("Duplicate slot_id %r across sections; keeping first",
                            slot.slot_id)
                continue
            all_slots[slot.slot_id] = slot
        # Any expected slot the model omitted gets a fallback too — so the
        # rendered report doesn't have silent gaps where the LLM was lazy.
        missing = [sid for sid in expected_ids if sid not in all_slots]
        if missing:
            _populate_fallbacks(
                all_slots, missing,
                "LLM omitted this slot from its response",
            )
        # Caveats from the model become section-level chips in the template.
        for i, c in enumerate(result.caveats):
            all_slots[f"_caveat_{section_id}_{i}"] = CommentarySlot(
                slot_id=f"_caveat_{section_id}_{i}",
                headline=c, body=c,
            )
    return all_slots
