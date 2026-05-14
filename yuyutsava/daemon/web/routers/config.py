"""Daemon config endpoints (events_config.json + watched directories).

GET  /config/events         — current events config
PATCH /config/events        — replace events config; triggers hot reload
POST /config/events/roots   — add a watched directory to the fs source
DELETE /config/events/roots?path=... — remove a watched directory
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from yuyutsava.core.config import EventsConfig, SourceConfig
from yuyutsava.daemon.web.deps import get_config_reload
from yuyutsava.daemon.web.schemas.config import (
    AddRootIn, EventsConfigOut, EventsConfigPatchIn, RootsOut, SourceDTO,
)
from yuyutsava.daemon.web.services.config_service import ConfigService

router = APIRouter(prefix="/config", tags=["config"])


def _to_dto(cfg: EventsConfig) -> EventsConfigOut:
    return EventsConfigOut(
        sources={
            name: SourceDTO(enabled=src.enabled, params=dict(src.params))
            for name, src in cfg.sources.items()
        },
    )


def _from_patch(patch: EventsConfigPatchIn) -> EventsConfig:
    sources = {
        name: SourceConfig(name=name, enabled=dto.enabled, params=dict(dto.params))
        for name, dto in patch.sources.items()
    }
    return EventsConfig(sources=sources)


def _service(request: Request) -> ConfigService:
    return ConfigService(reload_callback=get_config_reload(request))


@router.get("/events", response_model=EventsConfigOut, summary="Read events config")
async def get_events(svc: ConfigService = Depends(_service)) -> EventsConfigOut:
    return _to_dto(svc.get_events())


@router.patch("/events", response_model=EventsConfigOut, summary="Replace events config (hot reload)")
async def patch_events(
    body: EventsConfigPatchIn,
    svc: ConfigService = Depends(_service),
) -> EventsConfigOut:
    new_cfg = await svc.save_events(_from_patch(body))
    return _to_dto(new_cfg)


@router.post("/events/roots", response_model=RootsOut, summary="Add watched directory")
async def add_root(body: AddRootIn, svc: ConfigService = Depends(_service)) -> RootsOut:
    roots = await svc.add_root(body.path)
    return RootsOut(roots=roots)


@router.delete("/events/roots", response_model=RootsOut, summary="Remove watched directory")
async def remove_root(
    path: str = Query(..., description="Absolute path to remove from watched roots"),
    svc: ConfigService = Depends(_service),
) -> RootsOut:
    roots = await svc.remove_root(path)
    return RootsOut(roots=roots)
