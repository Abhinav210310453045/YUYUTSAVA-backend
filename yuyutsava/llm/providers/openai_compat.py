"""Every OpenAI-compatible host — ``ChatOpenAI`` pointed at a ``base_url``.

One shape serves five provider types: Groq, OpenRouter, Ollama, OpenAI itself,
and ``openai_compatible`` for any other server that speaks OpenAI's chat API
(xAI, DeepSeek, Together, Fireworks, a proxy, …). They differ in where they live
and how they log in, which the connection carries.

This is also the only provider with a reasoning toggle, and the one that can ask
OpenRouter for a model's live price.
"""

from __future__ import annotations

import logging
from typing import ClassVar

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions, Provider
from yuyutsava.llm.handle import Capability
from yuyutsava.llm.pricing import Price
from yuyutsava.llm.target import ModelTarget

logger = logging.getLogger(__name__)

#: ``ChatOpenAI`` requires a non-empty key; a server with no login gets this.
_NO_KEY = "ollama"

_PRICE_TIMEOUT_SECONDS = 6.0
_PER_MILLION = 1_000_000


def base_url_of(target: ModelTarget) -> str:
    """The OpenAI-compatible base URL for *target*, ``/v1`` included for Ollama."""
    base = target.connection.base_url.rstrip("/")
    if target.type == "ollama" and not base.endswith("/v1"):
        return base + "/v1"
    return base


def api_key_of(target: ModelTarget) -> str:
    """The key ``ChatOpenAI`` sends as ``Authorization: Bearer``."""
    auth = target.connection.auth
    if auth.method == "api_key":
        return target.secret(auth.key)
    if auth.method == "bearer_token":
        return target.secret(auth.token)
    return _NO_KEY


def _openrouter_headers(target: ModelTarget) -> dict[str, str]:
    connection = target.connection
    if target.type != "openrouter":
        return {}
    headers: dict[str, str] = {}
    if connection.http_referer:
        headers["HTTP-Referer"] = connection.http_referer
    if connection.app_title:
        headers["X-OpenRouter-Title"] = connection.app_title
    return headers


class OpenAICompatibleProvider(Provider):
    types: ClassVar[tuple[str, ...]] = (
        "groq",
        "openrouter",
        "ollama",
        "openai",
        "openai_compatible",
    )
    #: The only provider with a reasoning toggle — see ``build`` below.
    capabilities = frozenset({Capability.REASONING_TOGGLE})

    def build(self, target: ModelTarget, options: BuildOptions) -> BaseChatModel:
        headers = _openrouter_headers(target)
        # OpenRouter's unified reasoning control; ``enabled: false`` disables a
        # thinking model's reasoning so the full token budget goes to the answer.
        # Thinking models such as ``google/gemini-2.5-flash`` otherwise spend it on
        # reasoning tokens, truncating small structured outputs (e.g. the triage
        # decision JSON).
        extra_body = {"reasoning": {"enabled": False}} if options.disable_reasoning else None
        return ChatOpenAI(
            api_key=SecretStr(api_key_of(target)),
            base_url=base_url_of(target),
            model=target.model,
            temperature=options.temperature,
            max_completion_tokens=ANSWER_TOKENS,
            **({"default_headers": headers} if headers else {}),
            **({"extra_body": extra_body} if extra_body else {}),
        )

    def live_price(self, target: ModelTarget) -> Price | None:
        """OpenRouter publishes every model's price; a local Ollama costs nothing."""
        if target.type == "ollama":
            return (0.0, 0.0)
        if target.type != "openrouter":
            return None
        return _openrouter_price(base_url_of(target), api_key_of(target), target.model)


def _openrouter_price(base_url: str, api_key: str, model: str) -> Price | None:
    """``(in, out)`` per million tokens for *model* from OpenRouter's ``/models``, or None."""
    try:
        response = httpx.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=_PRICE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        listing = response.json().get("data") or []
    except (httpx.HTTPError, ValueError):
        logger.warning("pricing: OpenRouter /models fetch failed", exc_info=True)
        return None
    for entry in listing:
        if entry.get("id") != model:
            continue
        pricing = entry.get("pricing") or {}
        try:
            # OpenRouter reports US dollars *per token* as strings.
            return (
                float(pricing.get("prompt")) * _PER_MILLION,
                float(pricing.get("completion")) * _PER_MILLION,
            )
        except (TypeError, ValueError):
            logger.info("pricing: unparseable OpenRouter pricing for %r", model)
            return None
    logger.info("pricing: model %r not found in OpenRouter's catalog", model)
    return None


__all__ = ["OpenAICompatibleProvider", "api_key_of", "base_url_of"]
