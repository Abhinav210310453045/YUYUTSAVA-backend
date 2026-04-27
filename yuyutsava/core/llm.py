"""
Build a LangChain chat model for the configured provider.

Supported providers (set LLM_PROVIDER env var):
- groq       → ChatOpenAI pointed at Groq
- openrouter → ChatOpenAI pointed at OpenRouter
- anthropic  → ChatAnthropic (enables prompt caching via AnthropicPromptCachingMiddleware)

Groq:       https://console.groq.com/docs/overview
OpenRouter: https://openrouter.ai/docs/quickstart
Anthropic:  https://console.anthropic.com/
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from yuyutsava.core.config import AnthropicSettings, LlmSettings


def chat_model(settings: LlmSettings, *, temperature: float = 0.1) -> BaseChatModel:
    """Return a chat model for tool calling. Uses ChatAnthropic for AnthropicSettings, ChatOpenAI otherwise."""
    if isinstance(settings, AnthropicSettings):
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:
            raise RuntimeError(
                "langchain-anthropic is required for LLM_PROVIDER=anthropic. "
                "Run: pip install langchain-anthropic"
            ) from exc
        return ChatAnthropic(
            api_key=settings.api_key,
            model=settings.model,
            temperature=temperature,
            max_tokens=4096,
        )

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
