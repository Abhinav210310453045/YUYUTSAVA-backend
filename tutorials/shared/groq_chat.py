"""
Build a LangChain ``ChatOpenAI`` for OpenAI-compatible providers (Groq, OpenRouter, …).

- Groq: https://console.groq.com/docs/overview
- OpenRouter: https://openrouter.ai/docs/quickstart
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from tutorials.shared.config import TutorialLlmSettings


def groq_chat_model(settings: TutorialLlmSettings, *, temperature: float = 0.1) -> ChatOpenAI:
    """Return a chat model for tool calling (Groq, OpenRouter, or any matching settings)."""
    kwargs: dict[str, Any] = {
        "api_key": settings.api_key,
        "base_url": settings.base_url,
        "model": settings.model,
        "temperature": temperature,
        "max_tokens": 4096,
    }
    headers = getattr(settings, "default_headers", None)
    if headers:
        kwargs["default_headers"] = headers
    return ChatOpenAI(**kwargs)
