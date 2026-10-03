"""What a provider says the account can use: its models, and whether each one can be called.

:meth:`yuyutsava.llm.base.Provider.discover` asks a provider for its model list
through its free listing endpoints. Listing needs a working login, so a
discovery without a :attr:`Discovery.problem` is also the check that the login
works; the wizards and ``yuyutsava models discover`` show both.

Availability is what the provider says about *this account*, not the model in
general: a Claude model on Vertex whose quota is zero is listed, and marked as
needing a quota increase (design §2 rule 15); a Bedrock model whose agreement is
not accepted is marked as needing access. A provider that cannot tell says
:attr:`Availability.UNKNOWN`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from yuyutsava.llm.pricing import Price


class Availability(str, Enum):
    """Whether this account can call a model now."""

    AVAILABLE = "available"
    #: Listed, but the account's quota for it is zero.
    NEEDS_QUOTA = "needs_quota"
    #: Listed, but access must be granted first (an agreement, an enable step).
    NEEDS_ACCESS = "needs_access"
    #: Listed, but not served in the provider's region.
    NOT_IN_REGION = "not_in_region"
    #: The provider does not say.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DiscoveredModel:
    """One model a provider lists, with what it says about it."""

    #: The id to write in ``models.toml``.
    id: str
    #: The provider's own name for it, when it has one.
    label: str = ""
    context_window: int | None = None
    max_output: int | None = None
    #: Whether it can call tools, when the provider says.
    tools: bool | None = None
    price: Price | None = None
    availability: Availability = Availability.UNKNOWN
    #: Why it is not available, and what to do — shown next to it.
    note: str = ""


@dataclass(frozen=True)
class Discovery:
    """A provider's answer to "what can I use?"."""

    #: The provider's name under ``[providers]``.
    provider: str
    models: tuple[DiscoveredModel, ...] = ()
    #: Why the list could not be had — the login refused, the server unreachable.
    problem: str = ""
    #: What to do about :attr:`problem`.
    fix: str = ""
    #: Something to know that is not a failure, e.g. "add deployments by hand".
    note: str = ""

    @property
    def ok(self) -> bool:
        """Whether the provider answered — and so whether its login works."""
        return not self.problem


__all__ = ["Availability", "DiscoveredModel", "Discovery"]
