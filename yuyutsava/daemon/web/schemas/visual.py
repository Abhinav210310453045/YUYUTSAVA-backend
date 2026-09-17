"""Pydantic schemas for the visuals endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from yuyutsava.visuals.store import VisualRecord


class VisualOut(BaseModel):
    visual_id: str
    kind: str
    title: str | None
    mime: str
    url: str
    created_ts: float

    @classmethod
    def from_record(cls, r: VisualRecord) -> "VisualOut":
        return cls(
            visual_id=r.visual_id,
            kind=r.kind,
            title=r.title,
            mime=r.mime,
            url=f"/visuals/{r.visual_id}",
            created_ts=r.created_ts,
        )


class RenderIn(BaseModel):
    kind: str = Field(..., description="chart | diagram | table | code | math | timeline")
    spec: dict[str, Any] = Field(
        ..., description="Renderer spec (see docs/reference/visual-tools.md)"
    )
    thread_id: str = Field("rest", description="Thread/session to file the visual under")
