"""Stub LLM client — returns canned commentary, no API key required.

Used for:
  • CI / unit tests (no network)
  • End-to-end smoke before an OpenAI key is configured
  • Sanity checks that the pipeline is reachable even when the LLM is down

The canned content is deliberately generic — it satisfies the schema but
flags itself as a stub so an analyst reviewing the rendered report can
immediately tell that real commentary was not generated.
"""
from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from .schemas import CommentarySlot, SectionCommentary


T = TypeVar("T", bound=BaseModel)


class StubLLMClient:
    def generate_structured(
        self, *, system: str, user: str, schema: type[T],
    ) -> T:
        if schema is SectionCommentary:
            return schema(
                section_id="stub",
                slots=[
                    CommentarySlot(
                        slot_id="stub_default",
                        headline="[Stub] Commentary not generated — LLM disabled.",
                        body=(
                            "This is placeholder text from the StubLLMClient. "
                            "Configure ai_agent.llm.provider to 'openai' (Mac dev) "
                            "or 'gemini' (production) and provide an API key to "
                            "see real LLM-generated commentary in this slot."
                        ),
                    ),
                ],
                caveats=["LLM is in stub mode — content is not generated."],
            )
        # Fallback: instantiate with no fields. Will fail validation if the
        # caller passes a schema that requires fields — that's a useful
        # signal that you forgot to handle a new schema here.
        return schema()
