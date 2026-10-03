"""Gemini through Google AI Studio — an API key, ``ChatGoogleGenerativeAI``."""

from __future__ import annotations

from typing import ClassVar

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from yuyutsava.llm.base import BuildOptions, Provider
from yuyutsava.llm.discovery import Availability, DiscoveredModel, Discovery
from yuyutsava.llm.handle import Capability
from yuyutsava.llm.providers.gemini import build_gemini
from yuyutsava.llm.providers.listing import get_json
from yuyutsava.llm.target import ModelTarget, ProviderAccess

MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GoogleProvider(Provider):
    types: ClassVar[tuple[str, ...]] = ("google",)
    # genai caches an async client on the loop that first drives it — see quirks/loop_affinity.
    capabilities = frozenset({Capability.LOOP_AFFINE})

    def build(self, target: ModelTarget, options: BuildOptions) -> BaseChatModel:
        key = target.secret(target.connection.auth.key)
        return build_gemini(target, options, api_key=SecretStr(key))

    def discover(self, access: ProviderAccess) -> Discovery:
        """AI Studio's model list: the models that can generate, with their limits."""
        listing = get_json(
            MODELS_URL,
            label="Google AI Studio",
            headers={"x-goog-api-key": access.secret(access.connection.auth.key)},
            params={"pageSize": 1000},
        )
        if listing.problem:
            return Discovery(access.provider, problem=listing.problem, fix=listing.fix)
        return Discovery(access.provider, models=parse_ai_studio_models(listing.body))


def parse_ai_studio_models(body: dict) -> tuple[DiscoveredModel, ...]:
    """AI Studio's ``models.list``: the models that can ``generateContent``."""
    return tuple(
        DiscoveredModel(
            id=entry["name"].removeprefix("models/"),
            label=entry.get("displayName") or "",
            context_window=entry.get("inputTokenLimit"),
            max_output=entry.get("outputTokenLimit"),
            availability=Availability.AVAILABLE,
        )
        for entry in body.get("models") or []
        if "generateContent" in (entry.get("supportedGenerationMethods") or [])
    )


__all__ = ["GoogleProvider", "parse_ai_studio_models"]
