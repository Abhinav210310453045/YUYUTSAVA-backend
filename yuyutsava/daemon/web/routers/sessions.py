"""HTTP endpoints for persistent CLI sessions.

Backed by the same ``SqliteSessionStore`` the CLI writes to. The daemon and CLI
share one SQLite file via WAL; ``get_default_session_store`` is a process
singleton so no app-state wiring is needed.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from yuyutsava.daemon.web.schemas.session import SessionOut
from yuyutsava.sessions import (
    SessionNotFound,
    SessionsSettings,
    build_checkpointer,
    get_default_session_store,
)

router = APIRouter(tags=["sessions"])


@router.get("/sessions", response_model=list[SessionOut], summary="List persisted sessions")
async def list_sessions(
    workspace: str | None = Query(None, description="Filter to a single workspace path"),
    limit: int = Query(50, ge=1, le=500),
    cursor: float | None = Query(
        None, description="updated_at of the last row of the previous page (keyset pagination)"
    ),
) -> list[SessionOut]:
    store = get_default_session_store()
    ws = Path(workspace).resolve() if workspace else None
    rows = await store.list(workspace=ws, limit=limit, cursor=cursor)
    return [SessionOut.from_session(s) for s in rows]


@router.get("/sessions/{session_id}", response_model=SessionOut, summary="Fetch one session")
async def get_session(session_id: str) -> SessionOut:
    store = get_default_session_store()
    try:
        return SessionOut.from_session(await store.get(session_id))
    except SessionNotFound:
        raise HTTPException(status_code=404, detail=f"no session with id {session_id!r}")


@router.delete("/sessions/{session_id}", summary="Delete a session + its checkpoint rows")
async def delete_session(session_id: str) -> dict[str, int]:
    store = get_default_session_store()
    try:
        s = await store.get(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail=f"no session with id {session_id!r}")
    # Free both the metadata row and the LangGraph checkpoint rows.
    async with build_checkpointer(SessionsSettings.from_env()) as saver:
        await saver.adelete_thread(s.thread_id)
    await store.delete(session_id)
    return {"deleted": 1}
