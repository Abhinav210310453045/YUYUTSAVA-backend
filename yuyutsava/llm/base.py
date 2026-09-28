"""The provider contract.

A :class:`Provider` owns everything about ONE LLM provider that is not pure
config: which optional package to import, how to name its constructor kwargs, and
which wire-format quirks its SDK needs corrected.

That split matters. :mod:`yuyutsava.llm.settings` describes how to *reach* a
provider (``VertexSettings.project``/``location``) — frozen data that imports only
``yuyutsava.config``, so any layer can read it without loading an SDK. This module
describes how to *build and correct* it. They change for different reasons:
settings change when you switch project or region, a provider module changes when
an SDK ships (or fixes) a quirk. Keeping them apart keeps the dependency one-way —
providers import settings, never the reverse.

Adding a provider = one module here + one line in ``providers/__init__.py``.
Adding a new per-provider *concern* (prompt-caching support, a model-name
accessor, a structured-output strategy) = one method on this ABC **with a
default**, which providers override only if it applies to them. That is the
property that makes this scale on both axes; a flag on every Settings class, or a
parallel registry per concern, would not.
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from types import ModuleType
from typing import ClassVar

from langchain_core.language_models import BaseChatModel

from yuyutsava.llm.handle import Capability
from yuyutsava.llm.settings import LlmSettings

#: What every provider lets the model write for one reply — the answer or tool
#: call itself. A model that thinks gets its thinking budget *on top* of this (see
#: ``providers/gemini_budget``), so a long think can never leave the answer
#: without room.
ANSWER_TOKENS = 8192


def require(module: str, *, provider: str, install: str) -> ModuleType:
    """Import an optional provider SDK, or explain how to install it.

    Every native-SDK provider is behind a pip extra, so each one repeated the same
    try/ImportError/RuntimeError block. The dist name in the message is derived
    from the module name (``langchain_google_vertexai`` → ``langchain-google-vertexai``),
    which holds for every provider we ship.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise RuntimeError(
            f"{module.replace('_', '-')} is required for LLM_PROVIDER={provider}. "
            f"Run: pip install {install}"
        ) from exc


class Provider(ABC):
    """One LLM provider: how to build its chat model, quirks included."""

    #: The settings dataclass this provider serves. ``None`` marks the fallback
    #: provider, which is chosen by exclusion rather than by isinstance.
    settings_type: ClassVar[type | None] = None

    #: Stable identifier, recorded on every :class:`~yuyutsava.llm.handle.ModelHandle`
    #: this provider builds. Matches the ``LLM_PROVIDER`` value where one maps
    #: 1:1; :meth:`provider_name` refines it where one provider serves several
    #: hosts.
    key: ClassVar[str] = ""

    #: What callers may rely on for models this provider builds. Empty means
    #: "nothing beyond the base contract" — the common case, and why this is a
    #: default rather than an abstract member.
    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    @abstractmethod
    def build(
        self,
        settings: LlmSettings,
        *,
        temperature: float,
        disable_reasoning: bool,
    ) -> BaseChatModel:
        """Construct the chat model, with any of this provider's quirks applied.

        ``disable_reasoning`` is honoured only by providers that actually have a
        reasoning toggle; the rest ignore it (it reaches every provider because it
        is set per *call site*, not per provider). Declare
        :attr:`Capability.REASONING_TOGGLE` if you honour it.
        """

    def model_name(self, settings: LlmSettings) -> str:
        """The identifier of the model :meth:`build` will construct.

        Answered from *settings*, which is the only source that always has it.
        Reading it back off the built object was the previous approach and it
        returns ``""`` for Azure, whose SDK leaves ``model_name`` at ``None``.

        Every settings class carries ``model``, so the default serves all but
        Azure — which overrides it because there the deployment is authoritative.
        """
        return getattr(settings, "model", "") or ""

    def provider_name(self, settings: LlmSettings) -> str:
        """Which provider this is, for the handle and for logs.

        Overridden only by the OpenAI-compatible provider, where one
        implementation serves Groq, OpenRouter, Ollama and OpenAI and the
        implementation name would hide which host is actually being called.
        """
        return self.key


__all__ = ["Provider", "require"]
