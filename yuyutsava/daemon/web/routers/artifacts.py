"""HTTP serving for general (non-card) artifacts.

Serves the bytes an inline chat/voice artifact points at (its record carries a
relative ``/artifacts/{id}`` url). Read-only: artifacts are produced by the
``artifact_create`` tool and swept with their thread, never uploaded here.
Mirrors ``routers/todos.py``'s attachment-serve FileResponse pattern.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from yuyutsava.artifacts import store
from yuyutsava.daemon.web.bundle import bundle_asset_response

logger = logging.getLogger("yuyutsava.daemon.web.routers.artifacts")

router = APIRouter(tags=["artifacts"])


def _wire(rec: store.ArtifactRecordV1) -> dict:
    # `attachment_id` is the key the shared frontend block registry reads; alias
    # it onto the record so the same components render a gallery artifact and an
    # inline one identically (mirrors the artifact StreamEvent payload).
    return {**rec.model_dump(), "attachment_id": rec.artifact_id}


@router.get("/artifacts", summary="List general artifacts (newest first)")
async def list_artifacts(limit: int = Query(200, ge=1, le=1000)) -> list[dict]:
    recs = await asyncio.to_thread(store.list_records, limit)
    return [_wire(r) for r in recs]


@router.get("/artifacts/{artifact_id}", summary="Serve a general artifact's file")
async def get_artifact(
    artifact_id: str,
    download: bool = Query(False, description="Set Content-Disposition: attachment"),
) -> FileResponse:
    rec = store.load_record(artifact_id)
    if rec is None or not rec.path or not os.path.exists(rec.path):
        raise HTTPException(status_code=404, detail=f"no artifact {artifact_id!r}")
    return FileResponse(
        rec.path,
        media_type=rec.mime or "application/octet-stream",
        filename=os.path.basename(rec.path) if download else None,
    )


@router.get(
    "/artifacts/{artifact_id}/bundle/{rel_path:path}",
    summary="Serve a file from the artifact's own directory (multi-file artifacts)",
)
async def get_artifact_bundle_asset(artifact_id: str, rel_path: str) -> FileResponse:
    """Bytes for one file of a multi-file artifact — the twin of
    ``todos.get_attachment_bundle_asset``, so the shared frontend block registry
    renders an inline chat artifact and a card attachment identically."""
    rec = store.load_record(artifact_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"no artifact {artifact_id!r}")
    return bundle_asset_response(rec.path, rel_path)
