"""Setup endpoints — what the desktop app needs to make YUYUTSAVA work.

GET  /setup/state                  what is configured, what is missing, what can be started
GET  /setup/source                 which file settings are kept in
PUT  /setup/source                 keep them in the other one instead
PUT  /setup/settings               write settings to whichever file is in force
POST /setup/check/{name}           run one probe
POST /setup/services/{name}/start  start a container and wait until it answers
POST /setup/engine/start           start Docker Desktop and wait for its engine

What a setting *is* comes from ``/config/schema``, the same catalog the terminal
wizard reads; these carry only what a form cannot work out for itself.

Starting something is a POST and never happens on its own: the app shows the
command first and asks, exactly as the terminal does.
"""

from __future__ import annotations

from fastapi import APIRouter

from yuyutsava.daemon.web.schemas.setup import (
    CheckIn,
    CheckOut,
    SettingsIn,
    SettingsWrittenOut,
    SetupStateOut,
    SourceIn,
    SourceMovedOut,
    SourceStateOut,
)
from yuyutsava.daemon.web.services.setup_service import SetupService

router = APIRouter(prefix="/setup", tags=["setup"])

_service = SetupService()


@router.get("/state", response_model=SetupStateOut, summary="What is set up, and what is not")
async def get_state() -> SetupStateOut:
    """Everything the setup screen needs beyond the catalog itself.

    Runs no probe: reading files and asking Docker its version is quick, while
    waiting on Postgres is not, and a screen that cannot draw until a socket
    times out is worse than one that says "not checked yet".
    """
    return SetupStateOut(**_service.state())


@router.get("/source", response_model=SourceStateOut, summary="Which file settings are kept in")
async def get_source() -> SourceStateOut:
    return SourceStateOut(**_service.sources())


@router.put("/source", response_model=SourceMovedOut, summary="Keep settings in the other file")
async def put_source(body: SourceIn) -> SourceMovedOut:
    """Move every setting into ``settings.toml`` or into the ``.env``.

    Nothing is removed from where it came from before the new home has it, so a
    move that fails half way leaves both readable rather than neither.
    """
    return SourceMovedOut(**_service.move(body.kind, keep=body.keep))


@router.put("/settings", response_model=SettingsWrittenOut, summary="Write settings")
async def put_settings(body: SettingsIn) -> SettingsWrittenOut:
    """Write to whichever file is in force. An empty value removes a setting."""
    return SettingsWrittenOut(**_service.write(body.values))


@router.post("/check/{name}", response_model=CheckOut, summary="Does this actually work?")
async def post_check(name: str, body: CheckIn | None = None) -> CheckOut:
    """Run one probe by name. ``live`` is the only way anything is ever spent."""
    return CheckOut(**await _service.check(name, live=bool(body and body.live)))


@router.post(
    "/services/{name}/start", response_model=CheckOut, summary="Start a container and wait"
)
async def post_start(name: str) -> CheckOut:
    """Start a service and wait until its own probe answers, not merely until docker returns."""
    return CheckOut(**await _service.start(name))


@router.post("/engine/start", response_model=CheckOut, summary="Start Docker Desktop")
async def post_engine_start() -> CheckOut:
    """Start Docker Desktop, where there is one to start, and wait for its engine."""
    return CheckOut(**await _service.launch_engine())
