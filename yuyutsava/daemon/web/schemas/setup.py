"""Setup over HTTP: where settings live, whether they work, and starting services.

The desktop app asks the same questions the terminal wizard asks, from the same
catalog (:mod:`yuyutsava.config.catalog`, served by ``/config/schema``). These
carry the parts a form cannot work out for itself: which file is in force, what
is set in it, what a check said, and what starting a container did.

A value is never sent back out. A secret would be the obvious harm, but a
plain one is no better: the form overlays what the person typed, and anything
returned here would only be a second, staler copy of it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CheckOut(BaseModel):
    """What a probe found, in the shape every check in YUYUTSAVA answers with."""

    name: str = Field(description="What was probed, as a person would name it.")
    ok: bool
    problem: str = Field("", description="Why it did not work. Empty means it did.")
    fix: str = Field("", description="What to do about the problem.")
    note: str = Field("", description="Something worth knowing that is not a failure.")


class SourceOut(BaseModel):
    """One file settings can be kept in."""

    kind: str = Field(description="settings or env.")
    path: str
    exists: bool
    label: str = Field(description="How to name it to a person: always the path.")


class SourceStateOut(BaseModel):
    """Which file is in force, and what a person may switch to."""

    active: SourceOut
    choices: list[SourceOut]
    overlapping: list[str] = Field(
        default_factory=list,
        description="Names both files set. settings.toml wins; the .env ones are ignored.",
    )


class ServiceOut(BaseModel):
    """A container the setup can start."""

    name: str
    command: str = Field(description="Exactly what would be run, to show before running it.")
    purpose: str = Field(description="What it is for, for someone deciding whether to start it.")
    needed_by: list[str] = Field(
        default_factory=list, description="The settings that do not work without it."
    )


class SettingStateOut(BaseModel):
    """Whether one setting is set, and where from — never what it says."""

    key: str
    set: bool
    source: str = Field("", description="Which file it came from, or 'your shell'.")


class SetupStateOut(BaseModel):
    """Everything the setup screen needs that the catalog does not already say."""

    source: SourceStateOut
    settings: list[SettingStateOut]
    services: list[ServiceOut]
    engine: CheckOut = Field(description="Whether the container engine is running.")
    engine_launch_command: str = Field(
        "", description="What would start Docker Desktop; empty where there is no app to open."
    )
    models_configured: bool = Field(description="Whether models.toml exists and loads.")


class CheckIn(BaseModel):
    """Asking for one probe to run."""

    live: bool = Field(
        False,
        description="Prove an API key by really calling it, which spends one search credit. "
        "Never done unless asked for.",
    )


class SettingsIn(BaseModel):
    """Settings to write to whichever file is in force. An empty value removes one."""

    values: dict[str, str]


class SettingsWrittenOut(BaseModel):
    """What was written, and where."""

    written: list[str]
    path: str


class SourceIn(BaseModel):
    """Where settings should be kept from now on."""

    kind: str = Field(description="settings or env.")
    keep: bool = Field(False, description="Leave a copy where they are now as well.")


class SourceMovedOut(BaseModel):
    """What moved, and where it went."""

    moved: list[str]
    source: SourceStateOut
