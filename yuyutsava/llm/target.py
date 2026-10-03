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


@dataclass(frozen=True)
class ModelTarget:
    """One model, ready to build."""

    #: The provider's name under ``[providers]``.
    provider: str
    connection: Connection
    #: The model id as the provider names it.
    model: str
    #: The alias's settings, when the model was chosen through ``[models]``.
    entry: ModelEntry | None = None
    #: Resolved secrets, keyed by the reference as written. Never printed, and not
    #: part of a target's identity: two targets for the same model are equal.
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


__all__ = ["ModelTarget", "SecretMissing", "target_for"]
