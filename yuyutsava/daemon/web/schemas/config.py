"""Schemas for the /config/* endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FsParamsDTO(BaseModel):
    roots: list[str] = Field(default_factory=list)
    ignore: list[str] = Field(default_factory=list)
    coalesce_window_ms: int = 750


class SourceDTO(BaseModel):
    enabled: bool = True
    # Free-form params so we accept all sources without enumerating each.
    params: dict[str, Any] = Field(default_factory=dict)


class EventsConfigOut(BaseModel):
    sources: dict[str, SourceDTO]


class EventsConfigPatchIn(BaseModel):
    """Partial replacement: any source key replaces the entire source entry."""

    sources: dict[str, SourceDTO]


class AddRootIn(BaseModel):
    path: str = Field(..., description="Absolute directory path to add to the fs watcher")


class RootsOut(BaseModel):
    roots: list[str]
