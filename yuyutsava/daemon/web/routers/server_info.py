"""Server identity + capability flags (Phase 6 mobile contract).

One read-only endpoint the mobile app hits after the /health probe to learn
what this daemon can do, so a client built against the full /v1 contract
degrades gracefully on a daemon booted with features off (routing disabled,
SQLite-only memory, no resource governor, async subagents off).
"""

from __future__ import annotations

from importlib import metadata

from fastapi import APIRouter, Depends, Request

from yuyutsava.daemon.web.schemas.server_info import (
    CapabilitiesOut,
    ChannelInfoOut,
    ServerInfoOut,
)

router = APIRouter(tags=["server-info"])


def _package_version() -> str:
    try:
        return metadata.version("yuyutsava")
    except metadata.PackageNotFoundError:  # editable/source checkouts
        return "0.0.0+unknown"


def _get_capability_sources(request: Request) -> dict:
    """Collect the duck-typed singletons server-info reads. All optional —
    a missing one simply reads as 'capability off'."""
    state = request.app.state
    return {
        "model_router": getattr(state, "model_router", None),
        "memory_store": getattr(state, "memory_store", None),
        "resource_monitor": getattr(state, "resource_monitor", None),
        "async_subagents": bool(getattr(state, "async_subagents", False)),
        "channel_plugins": getattr(state, "channel_plugins", None),
    }


@router.get(
    "/server-info",
    response_model=ServerInfoOut,
    summary="Version + capability flags for graceful client degradation",
)
async def server_info(
    sources: dict = Depends(_get_capability_sources),
) -> ServerInfoOut:
    model_router = sources["model_router"]
    channel_plugins = sources["channel_plugins"]
    channels: list[ChannelInfoOut] = []
    if channel_plugins is not None:
        channels = [ChannelInfoOut(**row) for row in channel_plugins.snapshot()]
    return ServerInfoOut(
        version=_package_version(),
        capabilities=CapabilitiesOut(
            model_routing=bool(model_router is not None and model_router.enabled),
            memory=sources["memory_store"] is not None,
            resource_governor=sources["resource_monitor"] is not None,
            async_subagents=sources["async_subagents"],
        ),
        channels=channels,
    )
