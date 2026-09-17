"""Pydantic schemas for the runtime log-level endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LogLevelOut(BaseModel):
    level: str


class LogLevelIn(BaseModel):
    level: str = Field(..., description="One of DEBUG, INFO, WARNING")
