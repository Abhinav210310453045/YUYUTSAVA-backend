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

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from yuyutsava.llm.base import ANSWER_TOKENS, BuildOptions, Provider
from yuyutsava.llm.config.messages import type_label
from yuyutsava.llm.discovery import Availability, DiscoveredModel, Discovery
from yuyutsava.llm.handle import Capability
from yuyutsava.llm.pricing import Price
from yuyutsava.llm.providers.listing import get_json
from yuyutsava.llm.target import ModelTarget, ProviderAccess

logger = logging.getLogger(__name__)

#: ``ChatOpenAI`` requires a non-empty key; a server with no login gets this.
_NO_KEY = "ollama"

_PER_MILLION = 1_000_000

_NO_OLLAMA_CHAT_MODEL = (
    "Ollama is running but has no model that can chat — pull one, e.g. `ollama pull llama3.2`"
)


def base_url_of(target: ProviderAccess) -> str:
    """The OpenAI-compatible base URL for *target*, ``/v1`` included for Ollama."""
    base = target.connection.base_url.rstrip("/")
    if target.type == "ollama" and not base.endswith("/v1"):
        return base + "/v1"
    return base


def api_key_of(target: ProviderAccess) -> str:
    """The key ``ChatOpenAI`` sends as ``Authorization: Bearer``."""
    auth = target.connection.auth
    if auth.method == "api_key":
        return target.secret(auth.key)
    if auth.method == "bearer_token":
        return target.secret(auth.token)
    return _NO_KEY


def _openrouter_headers(target: ProviderAccess) -> dict[str, str]:
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

    def discover(self, access: ProviderAccess) -> Discovery:
        """The host's model list: ``/models``, or Ollama's ``/api/tags``."""
        if access.type == "ollama":
            server = base_url_of(access).removesuffix("/v1")
            listing = get_json(
                f"{server}/api/tags",
                label="Ollama",
                unreachable_fix="start Ollama (`ollama serve`), or check base_url",
            )
            parse = parse_ollama_tags
        else:
            listing = get_json(
                f"{base_url_of(access)}/models",
                label=type_label(access.type),
                headers={"Authorization": f"Bearer {api_key_of(access)}"},
            )
            parse = _PARSERS.get(access.type, parse_openai_models)
        if listing.problem:
            return Discovery(access.provider, problem=listing.problem, fix=listing.fix)
        models = parse(listing.body)
        if access.type == "ollama" and not models:
            return Discovery(access.provider, note=_NO_OLLAMA_CHAT_MODEL)
        return Discovery(access.provider, models=models)

    def live_price(self, target: ModelTarget) -> Price | None:
        """OpenRouter publishes every model's price; a local Ollama costs nothing."""
        if target.type == "ollama":
            return (0.0, 0.0)
        if target.type != "openrouter":
            return None
        discovery = self.discover(target)
        if not discovery.ok:
            logger.warning("pricing: %s", discovery.problem)
            return None
        found = next((m for m in discovery.models if m.id == target.model), None)
        if found is None:
            logger.info("pricing: model %r not found in OpenRouter's catalog", target.model)
        return found.price if found else None


def _per_million(value: object) -> float | None:
    try:
        return float(value) * _PER_MILLION
    except (TypeError, ValueError):
        return None


def parse_openrouter_models(body: dict) -> tuple[DiscoveredModel, ...]:
    """OpenRouter's ``/models``: window, output cap, price and tool support for each."""
    models = []
    for entry in body.get("data") or []:
        pricing = entry.get("pricing") or {}
        price_in, price_out = (
            _per_million(pricing.get("prompt")),
            _per_million(pricing.get("completion")),
        )
        top = entry.get("top_provider") or {}
        models.append(
            DiscoveredModel(
                id=entry["id"],
                label=entry.get("name") or "",
                context_window=entry.get("context_length") or top.get("context_length"),
                max_output=top.get("max_completion_tokens"),
                tools="tools" in (entry.get("supported_parameters") or []),
                price=(price_in, price_out)
                if price_in is not None and price_out is not None
                else None,
                availability=Availability.AVAILABLE,
            )
        )
    return tuple(models)


def parse_groq_models(body: dict) -> tuple[DiscoveredModel, ...]:
    """Groq's ``/models``: each model's window, and whether it is active."""
    return tuple(
        DiscoveredModel(
            id=entry["id"],
            context_window=entry.get("context_window"),
            availability=Availability.AVAILABLE
            if entry.get("active", True)
            else Availability.NEEDS_ACCESS,
            note="" if entry.get("active", True) else "Groq lists it as inactive",
        )
        for entry in body.get("data") or []
    )


def parse_openai_models(body: dict) -> tuple[DiscoveredModel, ...]:
    """An OpenAI-style ``/models``: ids only — the key that listed them can call them."""
    return tuple(
        DiscoveredModel(id=entry["id"], availability=Availability.AVAILABLE)
        for entry in body.get("data") or []
    )


def parse_ollama_tags(body: dict) -> tuple[DiscoveredModel, ...]:
    """Ollama's ``/api/tags``: the pulled models that can chat (embedding models are left out)."""
    models = []
    for entry in body.get("models") or []:
        capabilities = entry.get("capabilities")
        if capabilities is not None and "completion" not in capabilities:
            continue
        models.append(
            DiscoveredModel(
                id=entry["name"],
                tools=("tools" in capabilities) if capabilities is not None else None,
                price=(0.0, 0.0),
                availability=Availability.AVAILABLE,
            )
        )
    return tuple(models)


_PARSERS = {"openrouter": parse_openrouter_models, "groq": parse_groq_models}


__all__ = [
    "OpenAICompatibleProvider",
    "api_key_of",
    "base_url_of",
    "parse_groq_models",
    "parse_ollama_tags",
    "parse_openai_models",
    "parse_openrouter_models",
]
