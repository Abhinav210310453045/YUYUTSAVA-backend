"""The ``pending_asks`` wire format, shared by every backend.

Extracted from ``events/sqlite_backend.py`` when ``PendingAskStore`` was
collapsed onto the dialect adapter (ADR-002 step 2.5b). It lived there for the
same reason the twins did — SQLite came first — and after the migration the
*unified* store was importing its own column order out of a backend-specific
module. That is the dependency edge ADR-002 exists to remove: the wire format is
backend-independent, so it lives somewhere backend-independent.

``payload_json`` is authoritative — it is exactly what was broadcast — and the
flat columns exist for querying. Both are TEXT on Postgres too (not ``jsonb``),
so a spillover reconcile is a straight copy with no casts.
"""

from __future__ import annotations

import json
import time
from typing import Any


# Column order shared by the INSERT and the row→dict reader below.
_ASK_COLS = (
    "ask_id", "created_ts", "surface", "session_id", "thread_id", "card_id",
    "task_id", "interrupt_id", "agent_path", "agent_label", "title", "body",
    "options_json", "payload_json", "status", "answered_ts", "response",
)

def ask_row_to_record(row: Any) -> dict[str, Any]:
    """One ``pending_asks`` row → the wire record clients render.

    ``payload_json`` is authoritative (it is exactly what was broadcast); the
    flat columns exist for querying and are folded back in as a fallback for
    rows written by an older build.
    """
    try:
        payload = json.loads(row["payload_json"]) or {}
    except (ValueError, TypeError):
        payload = {}
    try:
        options = json.loads(row["options_json"]) or []
    except (ValueError, TypeError):
        options = []
    record = {
        "ask_id": row["ask_id"],
        "created_ts": row["created_ts"],
        "surface": row["surface"],
        "session_id": row["session_id"],
        "thread_id": row["thread_id"],
        "card_id": row["card_id"],
        "task_id": row["task_id"],
        "interrupt_id": row["interrupt_id"],
        "agent_path": row["agent_path"],
        "agent_label": row["agent_label"],
        "title": row["title"],
        "body": row["body"],
        "options": options,
    }
    record.update(payload)
    record["status"] = row["status"]
    return record


def ask_record_to_params(record: dict[str, Any]) -> tuple:
    """Wire record → the ``pending_asks`` column tuple (INSERT order)."""
    return (
        record.get("ask_id"),
        float(record.get("created_ts") or time.time()),
        record.get("surface") or "background",
        record.get("session_id"),
        record.get("thread_id"),
        record.get("card_id"),
        record.get("task_id"),
        record.get("interrupt_id"),
        record.get("agent_path"),
        record.get("agent_label"),
        record.get("title") or "",
        record.get("body") or "",
        json.dumps(list(record.get("options") or [])),
        json.dumps(record, default=str),
        "pending",
        None,
        None,
    )
