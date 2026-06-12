"""LLM client interface — provider-agnostic.

The interface deliberately accepts a Pydantic class and returns an instance
of it. Provider implementations are responsible for wiring up native
structured-output features (OpenAI's `response_format`, Gemini's response
schema) so the call site never has to parse raw strings.
"""
from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel


T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised on any LLM call failure: network, schema validation, auth."""


class LLMClient(Protocol):
    """All providers conform to this minimal interface.

    `generate_structured` takes a system prompt, user prompt, and a
    Pydantic class describing the expected output shape. Returns a
    validated instance of that class — never a raw dict. On any failure
    raises LLMError.
    """

    def generate_structured(
        self, *, system: str, user: str, schema: type[T],
    ) -> T:
        ...
