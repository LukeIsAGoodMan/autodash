"""Pydantic schemas for LLM commentary output.

Every LLM call returns a `SectionCommentary` containing one or more
`CommentarySlot` items. Each slot's `slot_id` corresponds to an anchor in
the Jinja template (chart name like 'overall_combo', or a section-level
id like 'section_a_summary'). The template looks up commentary by slot_id
at render time; missing slots fall back to a "Commentary pending"
placeholder so a partial LLM response can never crash the report.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CommentarySlot(BaseModel):
    """One narrative slot tied to a chart or section summary.

    `headline`: 6-15 words, one declarative sentence describing what moved.
    `body`: 40-120 words of structured prose. MUST cite specific numbers
            from the provided facts. NO causal language without explicit
            evidence (e.g. PAF event) — stick to descriptions.
    `material_movers` / `noise_flags`: forced materiality call so the LLM
            has to commit to a ranking rather than just listing numbers.
    """
    slot_id: str
    headline: str = Field(..., min_length=4, max_length=200)
    body: str = Field(..., min_length=20, max_length=1200)
    material_movers: list[str] = Field(
        default_factory=list,
        description=(
            "Up to 4 short phrases naming the movers the analyst should "
            "focus on. Order from most to least material. Empty list is "
            "fine when the slot is a pure trend chart with no ranking, "
            "or when everything looks like noise."
        ),
    )
    noise_flags: list[str] = Field(
        default_factory=list,
        description=(
            "Up to 4 short phrases naming movers that look big at first "
            "glance but should be DEMOTED — small cell, partial-month, "
            "single-month spike that reverses a trend. Empty is fine."
        ),
    )


class SectionCommentary(BaseModel):
    """A section's commentary bundle. The LLM returns this per section."""
    section_id: str
    slots: list[CommentarySlot] = Field(default_factory=list)
    # Caveats are surfaced at the top of the section in a yellow note box
    # — for "this month is partial, treat NRR as preliminary" style warnings.
    caveats: list[str] = Field(default_factory=list)
