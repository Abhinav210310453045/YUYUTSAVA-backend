"""Pydantic schemas for the CLI attach/detach endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class CliAttachIn(BaseModel):
    session_id: str | None = None
    label: str | None = None  # purely informational; logged for triage


class CliAttachOut(BaseModel):
    ok: bool
    channel_name: str
    attached: bool  # True if newly registered, False if already present


class CliDetachIn(BaseModel):
    session_id: str | None = None


class CliDetachOut(BaseModel):
    """``/cli/detach``'s reply. Distinct from ``proposal.OkOut``, which carries a
    default and an optional note — this one is a bare required flag."""

    ok: bool
