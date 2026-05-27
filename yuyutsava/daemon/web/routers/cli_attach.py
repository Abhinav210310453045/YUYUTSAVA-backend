"""CLI Mode 2: attach/detach endpoints.

A CLI process attaches to a running daemon by POSTing ``/cli/attach``. The
daemon registers a ``CliRemoteChannel`` with its ``ChannelRouter`` (idempotent
— if one is already attached, the existing instance is reused). The CLI then
subscribes to the existing ``/stream`` SSE endpoint and POSTs ``/ask/{id}/respond``
to answer Tier-2 questions, exactly the way the Electron renderer does.

When the CLI is the originator of a session (e.g. it submitted the task), it
passes ``session_id`` so the daemon's ``SessionOriginMap`` can prefer the CLI
for any HITL coming back from that session. Without ``session_id``, the CLI
is still attached but won't be preferred — it'll act as a passive observer.

POST /cli/detach removes the channel and unmaps the session.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from yuyutsava.daemon.cli_remote_channel import CliRemoteChannel
from yuyutsava.daemon.web.deps import (
    get_channels,
    get_hub,
    get_session_origin,
)
from yuyutsava.daemon.web.exceptions import ServiceUnavailableError

logger = logging.getLogger("yuyutsava.daemon.web.routers.cli_attach")

router = APIRouter(tags=["cli"])

# Module-level state — single shared CliRemoteChannel for v1.
_CLI_CHANNEL_NAME = "cli-remote"


class CliAttachIn(BaseModel):
    session_id: str | None = None
    label: str | None = None     # purely informational; logged for triage


class CliAttachOut(BaseModel):
    ok: bool
    channel_name: str
    attached: bool       # True if newly registered, False if already present


class CliDetachIn(BaseModel):
    session_id: str | None = None


class OkOut(BaseModel):
    ok: bool


def _find_cli_channel(channels) -> CliRemoteChannel | None:
    for c in channels.channels:
        if c.name == _CLI_CHANNEL_NAME and isinstance(c, CliRemoteChannel):
            return c
    return None


@router.post(
    "/cli/attach",
    response_model=CliAttachOut,
    summary="Register a CLI session as a HITL channel",
)
async def attach(
    body: CliAttachIn,
    channels=Depends(get_channels),
    session_origin=Depends(get_session_origin),
    hub=Depends(get_hub),
) -> CliAttachOut:
    if channels is None:
        raise ServiceUnavailableError("ChannelRouter not initialized")
    existing = _find_cli_channel(channels)
    if existing is None:
        channels.channels.append(CliRemoteChannel(hub, name=_CLI_CHANNEL_NAME))
        attached = True
        logger.info("CLI attached (label=%s session_id=%s)", body.label, body.session_id)
    else:
        attached = False
    if body.session_id and session_origin is not None:
        session_origin.set(body.session_id, _CLI_CHANNEL_NAME)
    return CliAttachOut(ok=True, channel_name=_CLI_CHANNEL_NAME, attached=attached)


@router.post(
    "/cli/detach",
    response_model=OkOut,
    summary="Unregister the CLI HITL channel",
)
async def detach(
    body: CliDetachIn,
    channels=Depends(get_channels),
    session_origin=Depends(get_session_origin),
) -> OkOut:
    if channels is None:
        raise ServiceUnavailableError("ChannelRouter not initialized")
    if body.session_id and session_origin is not None:
        session_origin.clear(body.session_id)
    cli = _find_cli_channel(channels)
    if cli is not None:
        channels.channels.remove(cli)
        logger.info("CLI detached")
    return OkOut(ok=True)
