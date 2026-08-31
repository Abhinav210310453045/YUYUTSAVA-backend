"""HTTP endpoints for rendered visuals (charts, diagrams, tables, ...).

Three surfaces over the same :class:`~yuyutsava.visuals.store.VisualStore`:

  * ``GET /sessions/{id}/visuals``      — list a session's visuals (Artifacts panel)
  * ``GET /visuals/{visual_id}``        — serve one image by its globally-unique id
  * ``POST /visuals/render``            — render + persist directly (standalone REST use)

The image-serving route mirrors ``GET /sessions/{id}/audio/{seq}`` in
``sessions.py``. The store is a process singleton (like the session store) so no
app-state wiring is required — it resolves the same ``state.db`` + blob files the
``vis_*`` tools write to.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from yuyutsava.storage.sessions import SessionNotFound, get_default_session_store
from yuyutsava.visuals.render import render
from yuyutsava.visuals.store import VisualRecord, get_default_visual_store
from yuyutsava.visuals.types import VisualError

logger = logging.getLogger("yuyutsava.daemon.web.routers.visuals")

router = APIRouter(tags=["visuals"])


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
    spec: dict[str, Any] = Field(..., description="Renderer spec (see docs/reference/visual-tools.md)")
    thread_id: str = Field("rest", description="Thread/session to file the visual under")


@router.get("/sessions/{session_id}/visuals", response_model=list[VisualOut],
            summary="List a session's rendered visuals")
async def list_session_visuals(session_id: str) -> list[VisualOut]:
    try:
        session = await get_default_session_store().get(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail=f"no session with id {session_id!r}")
    records = await get_default_visual_store().list_for_thread(session.thread_id)
    return [VisualOut.from_record(r) for r in records]


@router.get("/visuals/{visual_id}", summary="Serve a rendered visual image")
async def get_visual(visual_id: str) -> FileResponse:
    rec = await get_default_visual_store().get(visual_id)
    if rec is None or not os.path.exists(rec.path):
        raise HTTPException(status_code=404, detail=f"no visual with id {visual_id!r}")
    return FileResponse(rec.path, media_type=rec.mime)


@router.delete("/visuals/{visual_id}", summary="Delete a rendered visual (row + image file)")
async def delete_visual(visual_id: str) -> dict[str, Any]:
    """Erase a visual everywhere the agent saved it — the DB row and the on-disk
    image. A copy the user downloaded themselves lives elsewhere and is kept."""
    deleted = await get_default_visual_store().delete(visual_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"no visual with id {visual_id!r}")
    return {"ok": True, "visual_id": visual_id}


@router.post("/visuals/render", response_model=VisualOut,
             summary="Render a visual from a spec and persist it")
async def render_visual(body: RenderIn) -> VisualOut:
    try:
        result = render(body.kind, body.spec)
    except VisualError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    rec = await get_default_visual_store().save(result, body.thread_id)
    return VisualOut.from_record(rec)
