"""Channel plugin management endpoints (Phase 3).

``GET /channels`` lists known plugins with config + live state;
``POST /channels/{name}/enable|disable`` persists the flag to
``channels_config.json`` and applies it hot through the
ChannelPluginRegistry — no daemon restart.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from yuyutsava.daemon.web.deps import get_channel_plugins
from yuyutsava.daemon.web.exceptions import NotFoundError, ValidationError

logger = logging.getLogger("yuyutsava.daemon.web.routers.channels")

router = APIRouter(tags=["channels"])


class ChannelOut(BaseModel):
    name: str
    available: bool          # a plugin factory exists for this name
    enabled: bool            # config flag (survives restart)
    running: bool            # live right now
    capabilities: list[str]


class ChannelListOut(BaseModel):
    channels: list[ChannelOut]


class ChannelToggleOut(BaseModel):
    ok: bool
    name: str
    running: bool
    changed: bool            # False when the call was a no-op (idempotent)


@router.get(
    "/channels",
    response_model=ChannelListOut,
    summary="List channel plugins (config + live state)",
)
async def list_channels(registry=Depends(get_channel_plugins)) -> ChannelListOut:
    return ChannelListOut(
        channels=[ChannelOut(**row) for row in registry.snapshot()],
    )


def _persist_enabled(registry, name: str, enabled: bool) -> None:
    # Runs off-loop via asyncio.to_thread: to_file does mkdir + os.replace, which
    # blockbuster flags on the event loop (allow_blocking=False).
    new_cfg = registry.config.with_enabled(name, enabled)
    registry.set_config(new_cfg)
    new_cfg.to_file()


@router.post(
    "/channels/{name}/enable",
    response_model=ChannelToggleOut,
    summary="Enable a channel plugin (hot; persists to channels_config.json)",
)
async def enable_channel(
    name: str, registry=Depends(get_channel_plugins),
) -> ChannelToggleOut:
    try:
        changed = await registry.enable(name)
    except KeyError as exc:
        raise NotFoundError(f"unknown channel plugin {name!r}") from exc
    except ValueError as exc:
        # Misconfiguration (missing bot token, bad chat ids…) — the user
        # can fix env/config and retry without touching the daemon.
        raise ValidationError(str(exc)) from exc
    await asyncio.to_thread(_persist_enabled, registry, name, True)
    return ChannelToggleOut(ok=True, name=name, running=True, changed=changed)


@router.post(
    "/channels/{name}/disable",
    response_model=ChannelToggleOut,
    summary="Disable a channel plugin (hot; persists to channels_config.json)",
)
async def disable_channel(
    name: str, registry=Depends(get_channel_plugins),
) -> ChannelToggleOut:
    changed = await registry.disable(name)
    await asyncio.to_thread(_persist_enabled, registry, name, False)
    return ChannelToggleOut(ok=True, name=name, running=False, changed=changed)
