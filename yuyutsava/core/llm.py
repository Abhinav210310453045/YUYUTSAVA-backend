"""
Build a LangChain chat model for the configured provider.

Supported providers (set LLM_PROVIDER env var):
- groq       → ChatOpenAI pointed at Groq
- openrouter → ChatOpenAI pointed at OpenRouter
- anthropic  → ChatAnthropic (enables prompt caching via AnthropicPromptCachingMiddleware)
- ollama     → ChatOpenAI pointed at local Ollama server (http://localhost:11434/v1)

Groq:       https://console.groq.com/docs/overview
OpenRouter: https://openrouter.ai/docs/quickstart
Anthropic:  https://console.anthropic.com/
Ollama:     https://ollama.com/
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from yuyutsava.core.config import AnthropicSettings, LlmSettings


def model_name_of(model: BaseChatModel | object) -> str:
    """Best-effort model identifier for logging / usage rows.

    ``ChatOpenAI`` exposes ``model_name``, ``ChatAnthropic`` exposes
    ``model``; fakes and stubs typically expose neither → "".
    """
    for attr in ("model_name", "model"):
        v = getattr(model, attr, None)
        if isinstance(v, str) and v:
            return v
    return ""


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
            api_key=SecretStr(settings.api_key),
            model_name=settings.model,
            temperature=temperature,
            max_tokens_to_sample=4096
        )

    headers = getattr(settings, "default_headers", None)
    return ChatOpenAI(
        api_key=SecretStr(settings.api_key),
        base_url=settings.base_url,
        model=settings.model,
        temperature=temperature,
        max_completion_tokens=4096,
        **({"default_headers": headers} if headers else {}),
    )
