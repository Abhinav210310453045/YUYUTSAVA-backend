"""Pydantic schemas for the channel endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class ChannelOut(BaseModel):
    name: str
    available: bool  # a plugin factory exists for this name
    enabled: bool  # config flag (survives restart)
    running: bool  # live right now
    capabilities: list[str]


class ChannelListOut(BaseModel):
    channels: list[ChannelOut]


class ChannelToggleOut(BaseModel):
    ok: bool
    name: str
    running: bool
    changed: bool  # False when the call was a no-op (idempotent)
