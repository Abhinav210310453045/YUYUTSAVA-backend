"""The provider contract.

A :class:`Provider` owns everything about ONE type of LLM provider that is not
configuration: which optional package to import, how to log in with each login
method the type accepts, how to name its constructor kwargs, which wire-format
quirks its SDK needs corrected, and how to price its models.

Configuration — which provider, where, which model, which login — arrives as a
:class:`~yuyutsava.llm.target.ModelTarget` built from the models config
(docs/architecture/decisions/ADR-005-models-config.md). The split keeps the
dependency one-way: providers read targets, and nothing about a target needs an
SDK to describe.

Adding a provider type = one module here + one entry in ``providers/registry.py``
(and its connection in ``config/schema.py``). Adding a per-provider *concern* =
one method on this ABC **with a default**, which providers override only if it
applies to them.
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, ClassVar

from langchain_core.language_models import BaseChatModel

from yuyutsava.llm.discovery import Discovery
from yuyutsava.llm.handle import Capability
from yuyutsava.llm.pricing import Price

if TYPE_CHECKING:
    from yuyutsava.llm.target import ModelTarget, ProviderAccess

#: What every provider lets the model write for one reply — the answer or tool
#: call itself. A model that thinks gets its thinking budget *on top* of this (see
#: ``providers/gemini_budget``), so a long think can never leave the answer
#: without room.
ANSWER_TOKENS = 8192


@dataclass(frozen=True)
class BuildOptions:
    """How one call site wants its model, as opposed to which model it is."""

    temperature: float = 0.1
    #: Turn a thinking model's reasoning off. Honoured only by providers that
    #: declare :attr:`Capability.REASONING_TOGGLE`; the triage agent sets it
    #: because reasoning tokens truncate its small structured output.
    disable_reasoning: bool = False


def require(module: str, *, provider: str, install: str) -> ModuleType:
    """Import an optional provider SDK, or explain how to install it.

    Every native-SDK provider is behind a pip extra, so each one repeated the same
    try/ImportError/RuntimeError block. The dist name in the message is derived
    from the module name (``langchain_google_genai`` → ``langchain-google-genai``),
    which holds for every provider we ship.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise RuntimeError(
            f"{module.replace('_', '-')} is required for {provider} models. "
            f"Run: pip install {install}"
        ) from exc


class Provider(ABC):
    """One type of LLM provider: how to build its chat model, quirks included."""

    #: The connection ``type`` values this provider serves.
    types: ClassVar[tuple[str, ...]] = ()

    #: What callers may rely on for models this provider builds. Empty means
    #: "nothing beyond the base contract" — the common case, and why this is a
    #: default rather than an abstract member.
    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    @abstractmethod
    def build(self, target: ModelTarget, options: BuildOptions) -> BaseChatModel:
        """Construct the chat model for *target*, with this provider's quirks applied."""

    def model_name(self, target: ModelTarget) -> str:
        """The identifier usage rows and the task registry record for *target*'s model.

        The model id, unless the alias says it names a deployment or an inference
        profile, when the model behind it (``base_model``) is the true identifier.
        """
        if target.entry is not None and target.entry.base_model:
            return target.entry.base_model
        return target.model

    def provider_name(self, target: ModelTarget) -> str:
        """Which provider this is, for the handle, usage rows and logs: its type."""
        return target.type

    def discover(self, access: ProviderAccess) -> Discovery:
        """The models *access* can use, and whether each one can be called now.

        Listing needs a working login, so an answer without a problem is also
        the check that the login works. The default — for a provider with no
        listing endpoint — lists nothing and says so. Called off the event loop;
        makes network requests; never raises.
        """
        return Discovery(
            access.provider, note="this provider has no model list; add model ids under [models]"
        )

    def live_price(self, target: ModelTarget) -> Price | None:
        """*target*'s current price from the provider itself, when it publishes one.

        ``None`` — the default — means the provider has no price API, so the
        static table and the user's overrides are the source. Called off the event
        loop; may make a network request; never raises.
        """
        return None


__all__ = ["ANSWER_TOKENS", "BuildOptions", "Provider", "require"]
