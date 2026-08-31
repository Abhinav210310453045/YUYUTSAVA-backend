"""Runtime toggle endpoints (voice mode + dedicated subagent roster).

GET   /settings/runtime     — current snapshot of every hot switch
PATCH /settings/runtime     — flip one or more; broadcast to all surfaces
GET   /settings/subagents   — the dedicated subagent roster with enabled state

These are the *hot* switches, as opposed to ``/config/*`` (on-disk daemon
config, mostly restart-class). Every change is echoed onto the SSE stream as a
``settings`` item so the second renderer (voice overlay), mobile, and any other
connected client update without polling.

Turning wake-word detection off does more than set a flag: it flips
``sources.voice.enabled`` in ``events_config.json`` through the same
:class:`ConfigService` the Settings UI uses, so the hot reload tears down the
``_voice_proc`` child and the daemon actually *releases the microphone*. The
runtime pref is the single owner of that source's enabled bit.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Request

from yuyutsava.core.config import EventsConfig, SourceConfig
from yuyutsava.daemon.web.deps import (
    get_config_reload, get_hub, get_runtime_settings, get_subagent_roster,
)
from yuyutsava.daemon.web.exceptions import ValidationError
from yuyutsava.daemon.web.schemas.settings import (
    RuntimeSettingsOut, RuntimeSettingsPatchIn, SubagentDTO, SubagentRosterOut,
)
from yuyutsava.daemon.web.services.config_service import ConfigService
from yuyutsava.daemon.web.services.stream_service import StreamSettingsItem
from yuyutsava.prefs.runtime import UNDISABLEABLE

logger = logging.getLogger("yuyutsava.daemon.web.routers.settings")

router = APIRouter(prefix="/settings", tags=["settings"])


async def _apply_wake_to_events_config(request: Request, wake_enabled: bool) -> None:
    """Mirror the wake switch onto the ``voice`` events source, hot.

    Best-effort: a daemon booted without the events config (headless/tests) or a
    reload failure must not fail the toggle — the flag itself is still stored,
    and the wake bridge drops relays defensively.
    """
    try:
        svc = ConfigService(reload_callback=get_config_reload(request))
        cfg = svc.get_events()
        current = cfg.sources.get("voice")
        if current is not None and current.enabled == wake_enabled:
            return
        params = dict(current.params) if current is not None else {}
        sources = dict(cfg.sources)
        sources["voice"] = SourceConfig(
            name="voice", enabled=wake_enabled, params=params,
        )
        await svc.save_events(EventsConfig(sources=sources))
        logger.info("settings: voice events source → enabled=%s", wake_enabled)
    except Exception:  # noqa: BLE001
        logger.warning(
            "settings: could not apply wake_enabled=%s to the events config",
            wake_enabled, exc_info=True,
        )


async def _broadcast(request: Request, settings) -> None:
    """Fan the new snapshot out to every connected surface. Never fatal."""
    try:
        hub = get_hub(request)
    except Exception:  # noqa: BLE001 — headless app, no hub
        return
    try:
        await hub.broadcast(StreamSettingsItem(
            settings=settings.snapshot(), ts=time.time(),
        ))
    except Exception:  # noqa: BLE001
        logger.debug("settings: broadcast failed", exc_info=True)


def _to_dto(settings) -> RuntimeSettingsOut:
    return RuntimeSettingsOut(**settings.snapshot())


@router.get("/runtime", response_model=RuntimeSettingsOut,
            summary="Read the hot runtime toggles")
async def get_runtime(settings=Depends(get_runtime_settings)) -> RuntimeSettingsOut:
    # force=True: a CLI slash command may have written the row behind our back.
    await settings.refresh(force=True)
    return _to_dto(settings)


@router.patch("/runtime", response_model=RuntimeSettingsOut,
              summary="Flip runtime toggles (applies immediately)")
async def patch_runtime(
    body: RuntimeSettingsPatchIn,
    request: Request,
    settings=Depends(get_runtime_settings),
) -> RuntimeSettingsOut:
    if body.voice is not None:
        voice = await settings.set_voice(
            wake_enabled=body.voice.wake_enabled,
            tts_enabled=body.voice.tts_enabled,
        )
        if body.voice.wake_enabled is not None:
            await _apply_wake_to_events_config(request, voice.wake_enabled)

    if body.subagents is not None:
        patch = body.subagents
        if patch.disabled is not None:
            bad = sorted(set(patch.disabled) & UNDISABLEABLE)
            if bad:
                raise ValidationError(
                    f"{', '.join(bad)} cannot be disabled — it backs the master's "
                    "delegation fallback"
                )
            await settings.set_disabled_subagents(patch.disabled)
        elif patch.name is not None:
            if patch.enabled is None:
                raise ValidationError("subagents.name requires subagents.enabled")
            if patch.name in UNDISABLEABLE and not patch.enabled:
                raise ValidationError(
                    f"{patch.name} cannot be disabled — it backs the master's "
                    "delegation fallback"
                )
            await settings.set_subagent_enabled(patch.name, patch.enabled)

    await _broadcast(request, settings)
    return _to_dto(settings)


@router.get("/subagents", response_model=SubagentRosterOut,
            summary="Dedicated subagent roster with enabled state")
async def get_subagents(
    settings=Depends(get_runtime_settings),
    roster=Depends(get_subagent_roster),
) -> SubagentRosterOut:
    """The list the Settings UI renders as toggle rows.

    Built from the live ``BaseSubAgent`` instances the daemon booted with, so a
    subagent added later shows up with no UI change. ``general-purpose`` is
    listed but not togglable (see ``UNDISABLEABLE``).
    """
    await settings.refresh(force=True)
    subs = settings.subagents()
    out: list[SubagentDTO] = []
    for name, agent in sorted((roster or {}).items()):
        out.append(SubagentDTO(
            name=name,
            description=(getattr(agent, "description", "") or "").strip(),
            enabled=subs.is_enabled(name),
            togglable=name not in UNDISABLEABLE,
            kind="both" if getattr(agent, "supports_async", False) else "sync",
        ))
    return SubagentRosterOut(subagents=out)
