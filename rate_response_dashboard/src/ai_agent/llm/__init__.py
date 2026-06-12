"""LLM commentary layer (Stage 2).

Provider-agnostic: a single `LLMClient` interface with concrete impls for
OpenAI (development on Mac) and Gemini Enterprise (production at the
company). Swapping providers is a one-line config change; no analysis code
needs to know which model is talking.

Outputs are Pydantic-validated so a misbehaving LLM (truncation, malformed
JSON, schema drift) surfaces as a typed error at the boundary, not as a
silent broken commentary downstream.
"""
