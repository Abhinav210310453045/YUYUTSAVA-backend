"""HTTP endpoints for rendered visuals (charts, diagrams, tables, ...).

Three surfaces over the same :class:`~yuyutsava.visuals.store.VisualStore`:

  * ``GET /sessions/{id}/visuals``      — list a session's visuals (Artifacts panel)
  * ``GET /visuals/{visual_id}``        — serve one image by its globally-unique id
  * ``POST /visuals/render``            — render + persist directly (standalone REST use)

The image-serving route mirrors ``GET /sessions/{id}/audio/{seq}`` in
``sessions.py``. The store is the daemon's (``app.state.visual_store``), the one
the ``vis_*`` tools write to.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from yuyutsava.daemon.web.deps import get_session_store, get_visual_store
from yuyutsava.daemon.web.schemas.visual import RenderIn, VisualOut
from yuyutsava.storage.sessions import SessionNotFound
from yuyutsava.visuals.render import render
from yuyutsava.visuals.types import VisualError

logger = logging.getLogger("yuyutsava.daemon.web.routers.visuals")

router = APIRouter(tags=["visuals"])


@router.get(
    "/sessions/{session_id}/visuals",
    response_model=list[VisualOut],
    summary="List a session's rendered visuals",
)
async def list_session_visuals(
    session_id: str,
    sessions=Depends(get_session_store),
    visuals=Depends(get_visual_store),
) -> list[VisualOut]:
    try:
        session = await sessions.get(session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no session with id {session_id!r}") from exc
    records = await visuals.list_for_thread(session.thread_id)
    return [VisualOut.from_record(r) for r in records]


@router.get("/visuals/{visual_id}", summary="Serve a rendered visual image")
async def get_visual(visual_id: str, visuals=Depends(get_visual_store)) -> FileResponse:
    rec = await visuals.get(visual_id)
    if rec is None or not os.path.exists(rec.path):
        raise HTTPException(status_code=404, detail=f"no visual with id {visual_id!r}")
    return FileResponse(rec.path, media_type=rec.mime)


@router.delete("/visuals/{visual_id}", summary="Delete a rendered visual (row + image file)")
async def delete_visual(visual_id: str, visuals=Depends(get_visual_store)) -> dict[str, Any]:
    """Erase a visual everywhere the agent saved it — the DB row and the on-disk
    image. A copy the user downloaded themselves lives elsewhere and is kept."""
    deleted = await visuals.delete(visual_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"no visual with id {visual_id!r}")
    return {"ok": True, "visual_id": visual_id}


@router.post(
    "/visuals/render",
    response_model=VisualOut,
    summary="Render a visual from a spec and persist it",
)
async def render_visual(body: RenderIn, visuals=Depends(get_visual_store)) -> VisualOut:
    try:
        result = render(body.kind, body.spec)
    except VisualError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rec = await visuals.save(result, body.thread_id)
    return VisualOut.from_record(rec)
