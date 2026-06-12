"""OpenAI implementation of LLMClient.

Uses the structured-output `parse` helper on the chat completions API,
which guarantees a JSON response conforming to the supplied Pydantic
class. Any deviation surfaces as an OpenAI API error or a Pydantic
validation error; both are wrapped in `LLMError` so the caller sees a
single failure type.

API key is read from the env var named in cfg.ai_agent.llm.api_key_env
(default OPENAI_API_KEY). The key never enters logs, exceptions, or
serialized state.
"""
from __future__ import annotations

import logging
import os
from typing import TypeVar

from pydantic import BaseModel

from .client import LLMError


log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class OpenAIClient:
    def __init__(
        self,
        *,
        api_key_env: str = "OPENAI_API_KEY",
        model: str = "gpt-4o",
        timeout_seconds: float = 30.0,
    ) -> None:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise LLMError(
                f"Environment variable {api_key_env} is not set. "
                f"Set it before invoking the report generator."
            )
        # Import locally so the rest of the package does not require
        # the openai SDK at import time (useful for stub-only setups).
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, timeout=timeout_seconds)
        self._model = model

    def generate_structured(
        self, *, system: str, user: str, schema: type[T],
    ) -> T:
        try:
            resp = self._client.chat.completions.parse(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=schema,
            )
        except Exception as e:
            raise LLMError(f"OpenAI request failed: {e}") from e

        parsed = resp.choices[0].message.parsed
        if parsed is None:
            # Model refused or returned content that couldn't be parsed.
            refusal = resp.choices[0].message.refusal
            raise LLMError(f"OpenAI returned no parsed output. Refusal={refusal!r}")
        return parsed
