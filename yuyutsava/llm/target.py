"""``ModelTarget`` — everything needed to build one chat model, and nothing else.

A target is one model reference from the models config (``defaults.model``, a
task's model, a ``/model`` choice) with everything it points at looked up: the
provider's connection, the model id as that provider names it, the overrides of
the alias it was chosen by, and the secrets the provider's login needs. A
provider builds a chat model from a target alone
(:meth:`yuyutsava.llm.base.Provider.build`).

It is frozen and holds no SDK object, so it can be passed between threads and
loops and kept as long as the config it came from.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from yuyutsava.llm.config.loader import ConfigIssue
from yuyutsava.llm.config.schema import (
    Connection,
    ModelEntry,
    ModelsConfig,
    connection_type,
    parse_model_ref,
)
from yuyutsava.llm.config.secrets import Secret


class SecretMissing(RuntimeError):
    """A provider needs a secret whose value could not be found."""


@dataclass(frozen=True, kw_only=True)
class ProviderAccess:
    """How to reach one provider: its connection and the secrets its login needs.

    What listing a provider's models or checking its login takes; a
    :class:`ModelTarget` is this plus one model.
    """

    #: The provider's name under ``[providers]``.
    provider: str
    connection: Connection
    #: Resolved secrets, keyed by the reference as written. Never printed, and not
    #: part of identity: two targets for the same model are equal.
    secrets: Mapping[str, Secret] = field(
        default_factory=lambda: MappingProxyType({}), repr=False, compare=False
    )
    #: References whose value could not be found, and why.
    unavailable: Mapping[str, ConfigIssue] = field(
        default_factory=lambda: MappingProxyType({}), repr=False, compare=False
    )

    @property
    def type(self) -> str:
        """The provider's ``type`` (``vertex``, ``groq``, …)."""
        return connection_type(type(self.connection))

    def secret(self, reference: str) -> str:
        """The value behind *reference*, for the one call that hands it to an SDK."""
        found = self.secrets.get(reference)
        if found is not None:
            return found.reveal()
        issue = self.unavailable.get(reference)
        detail = f"{issue.problem} — {issue.fix}" if issue else f"{reference} was not resolved"
        raise SecretMissing(f"provider {self.provider!r} cannot log in: {detail}")


@dataclass(frozen=True, kw_only=True)
class ModelTarget(ProviderAccess):
    """One model, ready to build."""

    #: The model id as the provider names it.
    model: str
    #: The alias's settings, when the model was chosen through ``[models]``.
    entry: ModelEntry | None = None

    @property
    def family(self) -> str:
        """Which family serves the model on Vertex: ``gemini``, ``anthropic`` or ``maas``.

        The alias's ``family`` when it sets one; otherwise read from the id —
        ``claude-…`` is Claude, ``<publisher>/<name>-maas`` an open model served
        as a service, anything else Gemini. Only Vertex serves several families;
        for every other provider type this is its one family, the type itself.
        """
        if self.type != "vertex":
            return self.type
        if self.entry is not None and self.entry.family:
            return self.entry.family
        if self.model.startswith("claude"):
            return "anthropic"
        if "/" in self.model and self.model.endswith("-maas"):
            return "maas"
        return "gemini"


def access_for(
    config: ModelsConfig,
    provider: str,
    *,
    secrets: Mapping[str, Secret],
    unavailable: Mapping[str, ConfigIssue],
) -> ProviderAccess:
    """How to reach the provider named *provider* in *config*."""
    return ProviderAccess(
        provider=provider,
        connection=config.providers[provider],
        secrets=MappingProxyType(dict(secrets)),
        unavailable=MappingProxyType(dict(unavailable)),
    )


def target_for(
    config: ModelsConfig,
    reference: str,
    *,
    secrets: Mapping[str, Secret],
    unavailable: Mapping[str, ConfigIssue],
) -> ModelTarget:
    """The target *reference* names in *config*: an alias, or ``provider/model-id``.

    *config* has been through the loader, so every reference in it resolves;
    a *reference* from elsewhere that does not raises :class:`KeyError`.
    """
    ref = parse_model_ref(reference)
    if ref.provider is None:
        entry = config.models[ref.name]
        provider, model = entry.provider, entry.id
    else:
        entry, provider, model = None, ref.provider, ref.name
    return ModelTarget(
        provider=provider,
        connection=config.providers[provider],
        model=model,
        entry=entry,
        secrets=MappingProxyType(dict(secrets)),
        unavailable=MappingProxyType(dict(unavailable)),
    )


__all__ = ["ModelTarget", "ProviderAccess", "SecretMissing", "access_for", "target_for"]
