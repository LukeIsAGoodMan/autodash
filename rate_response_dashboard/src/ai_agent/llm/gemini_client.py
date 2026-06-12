"""Gemini Enterprise client — placeholder for company deployment.

When Gemini Enterprise access is provisioned, wire up `google-genai` (or
whatever the company's SDK package is) here. The interface contract is
the same as OpenAIClient — the writer / orchestrator code does not
change.

For now, instantiating this client raises immediately so a typo in
`provider:` doesn't silently fall back to anything unexpected.
"""
from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from .client import LLMError


T = TypeVar("T", bound=BaseModel)


class GeminiClient:
    def __init__(self, **kwargs) -> None:
        raise LLMError(
            "GeminiClient is not implemented yet. Use OpenAIClient on Mac, "
            "implement this when Gemini Enterprise access is provisioned."
        )

    def generate_structured(
        self, *, system: str, user: str, schema: type[T],
    ) -> T:  # pragma: no cover -- never reached
        raise NotImplementedError
