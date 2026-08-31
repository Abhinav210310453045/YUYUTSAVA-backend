"""Schemas for the /settings/* endpoints (runtime toggles).

Distinct from ``schemas/config.py``: those describe the *on-disk* daemon config
(``events_config.json`` + the ``.env`` catalog, most of it restart-class). These
are the hot switches every surface flips at will — voice mode and the dedicated
subagent roster.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class VoiceSettingsDTO(BaseModel):
    wake_enabled: bool = True
    tts_enabled: bool = True


class SubagentSettingsDTO(BaseModel):
    disabled: list[str] = Field(default_factory=list)


class RuntimeSettingsOut(BaseModel):
    voice: VoiceSettingsDTO
    subagents: SubagentSettingsDTO


class VoicePatchIn(BaseModel):
    """Partial voice patch — omitted fields keep their current value."""

    wake_enabled: bool | None = None
    tts_enabled: bool | None = None


class SubagentsPatchIn(BaseModel):
    """Either replace the whole deny-list, or flip one subagent by name."""

    disabled: list[str] | None = None
    name: str | None = Field(
        default=None, description="Single subagent to flip; pair with `enabled`"
    )
    enabled: bool | None = None


class RuntimeSettingsPatchIn(BaseModel):
    voice: VoicePatchIn | None = None
    subagents: SubagentsPatchIn | None = None


class SubagentDTO(BaseModel):
    """One entry in the roster the Settings UI renders as a toggle row."""

    name: str
    description: str = ""
    enabled: bool = True
    # False for structural agents (general-purpose) — rendered but not togglable.
    togglable: bool = True
    # "sync" (task tool) | "background" (start_async_task) | "both"
    kind: str = "sync"


class SubagentRosterOut(BaseModel):
    subagents: list[SubagentDTO]
