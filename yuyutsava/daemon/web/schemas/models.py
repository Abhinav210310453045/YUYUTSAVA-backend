"""Schemas for the ``/models`` endpoints: ``models.toml`` as the app reads and changes it.

Every change carries ``revision`` — the digest ``GET /models`` returned — so a
file changed elsewhere since is refused (409) rather than overwritten.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class IssueOut(BaseModel):
    path: str
    problem: str
    fix: str
    line: int | None = None


class TaskOut(BaseModel):
    name: str
    label: str
    reference: str
    #: True when the task has no model of its own and uses the default.
    follows_default: bool


class ProviderOut(BaseModel):
    name: str
    type: str
    label: str
    login: str
    settings: dict[str, Any] = Field(default_factory=dict)


class AliasOut(BaseModel):
    name: str
    provider: str
    id: str
    settings: dict[str, Any] = Field(default_factory=dict)


class MissingSecretOut(BaseModel):
    reference: str
    problem: str
    fix: str


class LeftoverOut(BaseModel):
    #: The variable, as set: ``LLM_PROVIDER``, ``TRIAGE_GROQ_MODEL``.
    name: str
    #: ``setting`` or ``key``.
    kind: str
    why: str
    #: The ``.env`` files that set it; empty when it is set elsewhere (the shell).
    files: list[str] = Field(default_factory=list)


class PresenceOut(BaseModel):
    #: Changing a key needs the owner confirmed present (``[security]``).
    required: bool = False
    #: What asks on this machine — "Touch ID or your password" — or None when nothing can.
    mechanism: str | None = None


class ModelsStatusOut(BaseModel):
    #: ``models.toml`` loads; without it, models come from the environment.
    configured: bool
    #: The daemon is waiting to be set up: only these routes answer.
    setup_mode: bool = False
    path: str
    revision: str
    exists: bool
    problem: str | None = None
    issues: list[IssueOut] = Field(default_factory=list)
    default: str | None = None
    tasks: list[TaskOut] = Field(default_factory=list)
    providers: list[ProviderOut] = Field(default_factory=list)
    models: list[AliasOut] = Field(default_factory=list)
    secrets_missing: list[MissingSecretOut] = Field(default_factory=list)
    #: The environment's model variables still set, which models.toml replaced.
    leftovers: list[LeftoverOut] = Field(default_factory=list)
    presence: PresenceOut = Field(default_factory=PresenceOut)


class FieldOut(BaseModel):
    name: str
    question: str
    default: str = ""
    secret: bool = False


class LoginFormOut(BaseModel):
    method: str
    label: str
    fields: list[FieldOut]


class ProviderFormOut(BaseModel):
    type: str
    label: str
    introduction: str
    settings: list[FieldOut]
    logins: list[LoginFormOut]


class FindingOut(BaseModel):
    name: str
    type: str
    label: str
    origin: str
    connection: dict[str, Any]
    model: str | None = None
    model_settings: dict[str, Any] = Field(default_factory=dict)


class DiscoveredOut(BaseModel):
    id: str
    label: str = ""
    context_window: int | None = None
    #: ``available``, ``needs_quota``, ``needs_access``, ``not_in_region`` or ``unknown``.
    availability: str
    note: str = ""


class DiscoveryOut(BaseModel):
    ok: bool
    problem: str = ""
    fix: str = ""
    note: str = ""
    checked_at: float
    models: list[DiscoveredOut] = Field(default_factory=list)


class CheckProviderIn(BaseModel):
    """A provider to log in to before it is saved. Secrets are kept in ``secrets.toml`` at once."""

    name: str
    connection: dict[str, Any]
    #: The login's secret fields, by field name (``key``, ``token``, …), as typed.
    secrets: dict[str, str] = Field(default_factory=dict)


class CheckProviderOut(DiscoveryOut):
    #: The connection as it would be written: secrets as ``secret:`` references.
    connection: dict[str, Any]


class NewProviderIn(BaseModel):
    name: str
    connection: dict[str, Any]


class NewAliasIn(BaseModel):
    name: str
    provider: str
    id: str
    location: str | None = None
    region: str | None = None
    context_window: int | None = None
    thinking: dict[str, Any] | None = None


class SetupIn(BaseModel):
    """A whole setup, written at once: providers, aliases, the default, and tasks."""

    revision: str
    providers: list[NewProviderIn] = Field(default_factory=list)
    aliases: list[NewAliasIn] = Field(default_factory=list)
    default: str | None = None
    #: Task → model reference, for tasks that should not follow the default.
    tasks: dict[str, str] = Field(default_factory=dict)


class SecurityIn(BaseModel):
    revision: str
    #: Whether changing a key needs the owner confirmed present.
    require_user_presence: bool


class DefaultIn(BaseModel):
    revision: str
    reference: str


class TasksIn(BaseModel):
    revision: str
    tasks: list[str]
    #: The model for those tasks; ``None`` puts them back on the default.
    reference: str | None = None


class SessionModelsIn(BaseModel):
    """A conversation's own models; ``""`` follows ``models.toml``."""

    model: str = ""
    #: Subagent name (``"*"`` for all) → model reference.
    subagents: dict[str, str] = Field(default_factory=dict)


class SessionModelsOut(SessionModelsIn):
    session_id: str
    #: What the conversation's next turn runs on.
    effective: str


class TestMessageIn(BaseModel):
    """One short message to a model — billable, and sent only when asked for."""

    reference: str


class TestMessageOut(BaseModel):
    ok: bool
    reference: str
    reply: str = ""
    seconds: float = 0.0
    problem: str = ""


class RemoveLeftoversIn(BaseModel):
    #: Which leftovers to remove from their ``.env`` files.
    names: list[str]
