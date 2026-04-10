"""
Load LLM settings from environment for tutorials (Groq or OpenRouter).

Both providers expose an OpenAI-compatible HTTP API:

- Groq: https://console.groq.com/docs/overview
- OpenRouter: https://openrouter.ai/docs/quickstart
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol


GROQ_OPENAI_BASE_URL = "https://api.groq.com/openai/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class TutorialLlmSettings(Protocol):
    """Structural type for ``ChatOpenAI``-compatible provider configs."""

    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True)
class GroqSettings:
    api_key: str
    base_url: str = GROQ_OPENAI_BASE_URL
    model: str = "llama-3.3-70b-versatile"

    @classmethod
    def from_env(cls) -> GroqSettings:
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "Set GROQ_API_KEY in the environment (or .env loaded by the CLI). "
                "See https://console.groq.com/docs/overview"
            )
        base = os.environ.get("GROQ_BASE_URL", GROQ_OPENAI_BASE_URL).strip() or GROQ_OPENAI_BASE_URL
        default_model = "llama-3.3-70b-versatile"
        model = os.environ.get("GROQ_MODEL", default_model).strip() or default_model
        return cls(api_key=key, base_url=base, model=model)


@dataclass(frozen=True)
class OpenRouterSettings:
    """OpenRouter unified API (OpenAI-compatible chat completions)."""

    api_key: str
    base_url: str = OPENROUTER_BASE_URL
    model: str = "openai/gpt-4o-mini"
    default_headers: dict[str, str] | None = None

    @classmethod
    def from_env(cls) -> OpenRouterSettings:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "Set OPENROUTER_API_KEY in the environment (or .env loaded by the CLI). "
                "See https://openrouter.ai/docs/quickstart"
            )
        base = os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL).strip() or OPENROUTER_BASE_URL
        default_model = "openai/gpt-4o-mini"
        model = os.environ.get("OPENROUTER_MODEL", default_model).strip() or default_model
        headers: dict[str, str] = {}
        referer = os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
        title = os.environ.get("OPENROUTER_APP_TITLE", "").strip()
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-OpenRouter-Title"] = title
        dh = headers if headers else None
        return cls(api_key=key, base_url=base, model=model, default_headers=dh)


def tutorial_llm_settings_from_env() -> TutorialLlmSettings:
    """Pick provider via ``LLM_PROVIDER`` (``groq`` or ``openrouter``; default ``groq``)."""
    provider = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
    if provider == "openrouter":
        return OpenRouterSettings.from_env()
    if provider == "groq":
        return GroqSettings.from_env()
    raise RuntimeError(
        f"Unknown LLM_PROVIDER={provider!r}; use 'groq' or 'openrouter'."
    )
