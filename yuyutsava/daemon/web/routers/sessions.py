"""HTTP endpoints for persistent CLI sessions.

Backed by the same session store the CLI writes to — the SQLite twin (shared
via WAL) in zero-config mode, or the Postgres ``sessions`` table when
``YUYUTSAVA_STORAGE_BACKEND=postgres`` (then the index JOINs threads/tasks/
usage). ``get_default_session_store`` is a process singleton, so no app-state
wiring is needed; the daemon injects its pooled :class:`PgSessionStore` at boot.

The message-history endpoints (Phase 6b) let a resumed UI/voice conversation
re-render its past turns (closing the "resume opens empty" gap) and replay the
agent's spoken audio. Voice threads read the dedicated ``voice_messages`` store
(text + audio refs); text chats read the verbatim ``transcript_messages``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from yuyutsava.daemon.web.schemas.session import SessionOut
from yuyutsava.storage.purge import purge_session
from yuyutsava.storage.sessions import (
    SessionNotFound,
    get_default_session_store,
)

logger = logging.getLogger("yuyutsava.daemon.web.routers.sessions")

router = APIRouter(tags=["sessions"])


@router.get("/sessions", response_model=list[SessionOut], summary="List persisted sessions")
async def list_sessions(
    workspace: str | None = Query(None, description="Filter to a single workspace path"),
    origin: str | None = Query(
        None, description="Filter by interface that created the session: 'cli', 'ui' or 'voice'"
    ),
    limit: int = Query(50, ge=1, le=500),
    cursor: float | None = Query(
        None, description="updated_at of the last row of the previous page (keyset pagination)"
    ),
) -> list[SessionOut]:
    store = get_default_session_store()
    ws = Path(workspace).resolve() if workspace else None
    rows = await store.list(workspace=ws, origin=origin, limit=limit, cursor=cursor)
    return [SessionOut.from_session(s) for s in rows]


@router.get("/sessions/{session_id}", response_model=SessionOut, summary="Fetch one session")
async def get_session(session_id: str) -> SessionOut:
    store = get_default_session_store()
    try:
        return SessionOut.from_session(await store.get(session_id))
    except SessionNotFound:
        raise HTTPException(status_code=404, detail=f"no session with id {session_id!r}")


def _extract_text(content: object) -> str:
    """Flatten a LangChain message ``content`` (str or content-block list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


@router.get("/sessions/{session_id}/messages", summary="Conversation history for resume/replay")
async def get_session_messages(session_id: str, request: Request) -> dict:
    """Return the ordered conversation turns so a resumed chat re-renders.

    Voice sessions return the ``voice_messages`` surface (text + an ``audio_url``
    for assistant turns with a stored clip). Text chats return the human/ai
    messages from the verbatim transcript store. Either way the shape is a flat
    ``messages`` list the UI can map straight onto bubbles.
    """
    store = get_default_session_store()
    try:
        session = await store.get(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail=f"no session with id {session_id!r}")

    thread_id = session.thread_id
    voice_store = getattr(request.app.state, "voice_store", None)
    transcript_store = getattr(request.app.state, "transcript_store", None)
    messages: list[dict] = []

    if session.origin == "voice" and voice_store is not None:
        rows = await voice_store.list_messages(thread_id)
        for m in rows:
            messages.append({
                "role": m.role,
                "text": m.text,
                "modality": m.modality,
                "has_audio": m.has_audio,
                "audio_url": (
                    f"/sessions/{session_id}/audio/{m.seq}" if m.has_audio else None
                ),
                "seq": m.seq,
                "ts": m.created_ts,
            })
    elif transcript_store is not None:
        rows = await transcript_store.list_messages(thread_id)
        for m in rows:
            if m.type not in ("human", "ai"):
                continue  # skip system/tool messages — not chat bubbles
            data = m.content.get("data", {}) if isinstance(m.content, dict) else {}
            text = _extract_text(data.get("content", ""))
            if not text.strip():
                continue  # tool-call-only ai turns carry no prose
            messages.append({
                "role": "user" if m.type == "human" else "assistant",
                "text": text,
                "modality": "text",
                "has_audio": False,
                "audio_url": None,
                "seq": m.seq,
                "ts": m.created_ts,
            })
        # Mixed text+voice chats (a tinker card, the master chat with the mic)
        # keep their prose in the transcript but their spoken replies in
        # voice_messages — reattach each stored clip to its transcript bubble
        # (matched by the turn's final text, each clip used once, in order) so
        # a reopened thread can still ▶ replay what the agent said. Best-effort:
        # history renders text-only if the voice surface is unavailable.
        if voice_store is not None:
            try:
                vrows = await voice_store.list_messages(thread_id)
            except Exception:  # noqa: BLE001
                vrows = []
            for v in vrows:
                if v.role != "assistant" or not v.has_audio:
                    continue
                vtext = (v.text or "").strip()
                for m in messages:
                    if (
                        m["role"] == "assistant"
                        and not m["has_audio"]
                        and m["text"].strip() == vtext
                    ):
                        m["has_audio"] = True
                        m["audio_url"] = f"/sessions/{session_id}/audio/{v.seq}"
                        m["modality"] = "audio"
                        break

    return {"session_id": session_id, "origin": session.origin, "messages": messages}


@router.get("/sessions/{session_id}/audio/{seq}", summary="Serve a turn's TTS audio clip")
async def get_session_audio(session_id: str, seq: int, request: Request) -> FileResponse:
    store = get_default_session_store()
    try:
        session = await store.get(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail=f"no session with id {session_id!r}")

    voice_store = getattr(request.app.state, "voice_store", None)
    if voice_store is None:
        raise HTTPException(status_code=404, detail="voice history not available")
    msg = await voice_store.get_message(session.thread_id, seq)
    if msg is None or not msg.audio_blob_path or not os.path.exists(msg.audio_blob_path):
        raise HTTPException(status_code=404, detail="no audio for that turn")
    return FileResponse(msg.audio_blob_path, media_type="audio/wav")


@router.delete("/sessions/{session_id}", summary="Delete a session + all its data")
async def delete_session(session_id: str) -> dict[str, int]:
    """Purge every row + file tied to the session (checkpoints, transcript,
    artifacts, summaries, voice history + audio blobs, tasks, usage,
    proposals/decisions, interrupts, and the ephemeral memories), then the
    session row itself. See :func:`yuyutsava.storage.purge.purge_session` for the
    full teardown + atomicity model."""
    try:
        report = await purge_session(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail=f"no session with id {session_id!r}")
    return {
        "deleted": 1,
        "rows_purged": report.total_rows,
        "voice_blobs_deleted": report.voice_blobs_deleted,
    }
