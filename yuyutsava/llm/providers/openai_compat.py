"""Every OpenAI-compatible host — ``ChatOpenAI`` pointed at a ``base_url``.

The fallback provider, chosen by exclusion rather than by settings type, because
one shape serves many hosts: Groq, OpenRouter, Ollama, OpenAI itself, and the
generic ``openai_compatible`` escape hatch (xAI, DeepSeek, Together, Fireworks,
Perplexity, …). They differ only in ``base_url``/``api_key``/``model``, which the
Settings classes already carry, so there is nothing per-host to encode here.

This is also the only provider with a reasoning toggle, which is why
``disable_reasoning`` lives here rather than being a generic knob that every other
provider had to document as a no-op.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from yuyutsava.core.config import LlmSettings
from yuyutsava.llm.base import Provider


class OpenAICompatibleProvider(Provider):
    settings_type = None  # the fallback — see providers/__init__.provider_for

    def build(
        self, settings: LlmSettings, *, temperature: float, disable_reasoning: bool
    ) -> BaseChatModel:
        headers = getattr(settings, "default_headers", None)
        # OpenRouter's unified reasoning control; ``enabled: false`` disables a
        # thinking model's reasoning so the full token budget goes to the answer.
        # Thinking models such as ``google/gemini-2.5-flash`` otherwise spend it on
        # reasoning tokens, truncating small structured outputs (e.g. the triage
        # decision JSON).
        extra_body = {"reasoning": {"enabled": False}} if disable_reasoning else None
        return ChatOpenAI(
            api_key=SecretStr(settings.api_key),
            base_url=settings.base_url,
            model=settings.model,
            temperature=temperature,
            max_completion_tokens=4096,
            **({"default_headers": headers} if headers else {}),
            **({"extra_body": extra_body} if extra_body else {}),
        )


__all__ = ["OpenAICompatibleProvider"]
