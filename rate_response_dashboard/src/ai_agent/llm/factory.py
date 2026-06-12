"""LLM client factory — single entry point that turns cfg into a client.

Provider can be overridden via the AI_REPORT_LLM_PROVIDER env var so you
can toggle openai/gemini/stub without editing config.yaml.
"""
from __future__ import annotations

import os

from .client import LLMClient, LLMError


def build_client(cfg: dict) -> LLMClient:
    """Read ai_agent.llm.provider and return the configured client.

    Defaults: provider='stub' (safe — no network, no spend).
    Set provider='openai' in config.yaml OR `AI_REPORT_LLM_PROVIDER=openai`
    and export OPENAI_API_KEY to get real commentary on Mac dev.
    """
    llm_cfg = (cfg.get("ai_agent") or {}).get("llm") or {}
    # Env override beats config so a single shell can flip providers.
    provider = (os.environ.get("AI_REPORT_LLM_PROVIDER")
                or llm_cfg.get("provider")
                or "stub").lower()
    model = os.environ.get("AI_REPORT_LLM_MODEL") or llm_cfg.get("model", "gpt-4o")
    api_key_env = llm_cfg.get("api_key_env", "OPENAI_API_KEY")
    timeout = float(llm_cfg.get("timeout_seconds", 30.0))

    if provider == "stub":
        from .stub_client import StubLLMClient
        return StubLLMClient()
    if provider == "openai":
        from .openai_client import OpenAIClient
        return OpenAIClient(api_key_env=api_key_env, model=model,
                            timeout_seconds=timeout)
    if provider == "gemini":
        from .gemini_client import GeminiClient
        return GeminiClient(api_key_env=api_key_env, model=model,
                            timeout_seconds=timeout)
    raise LLMError(
        f"Unknown LLM provider {provider!r}. Set ai_agent.llm.provider to "
        f"one of: stub, openai, gemini."
    )
