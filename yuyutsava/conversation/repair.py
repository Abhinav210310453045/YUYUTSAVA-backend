"""Make a conversation's message history valid again after an interruption.

## The failure this exists for

A user sent a chat message while ``tr_run_python`` was in flight. LangGraph
cancelled the tool task and fabricated a replacement result:

    ToolMessage(status="success",
                content="Tool call <id> was cancelled - another message came "
                        "in before it could be completed.")

From that point the thread was **wedged**. Every subsequent call returned
``finish_reason: STOP`` with ``output_tokens: 0``, empty content and no tool
calls, and the provider's prompt cache went from 65,882 read tokens to 0 —
four consecutive user messages, ~70,000 input tokens each, and silence. The
conversation could not be rescued by sending another message, because every
message made the same malformed history longer.

Two things are wrong with that fabricated result and both matter:

* ``status="success"`` with the word "cancelled" in the body tells a model the
  call *worked*. That produces confident hallucinations — "I wrote the file"
  when nothing was written.
* a tool result that answers a tool call the model no longer has context for
  leaves the history in a shape some providers will not continue from at all.

## What this does

Rewrite every such result as an explicit ``status="error"`` with a body that
says what actually happened, preserving ``id`` so LangGraph's ``add_messages``
reducer merges in place rather than appending a duplicate.

The wording differs by cause, because the model should draw different
conclusions:

* :data:`Cause.INTERRUPTED_BY_USER` — the user interjected. The call never
  ran, nothing changed, and the right move is usually to read the new message
  and decide whether the work is still wanted.
* :data:`Cause.SESSION_ENDED` — a permission prompt died with the process
  (Ctrl+C, terminal close, crash). Consent was never given, so the action must
  be re-proposed rather than assumed.

Previously this lived as a private helper in ``sessions/runner.py`` and ran
**only** on CLI ``--resume``. The daemon — app and voice — never called it,
which is why that thread stayed wedged forever.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

logger = logging.getLogger("yuyutsava.conversation.repair")

#: The text LangGraph puts in the result it fabricates for a cancelled tool
#: task. Matching on it is unavoidable — it is the only marker we get — so it
#: lives here, once, with a test that fails loudly if the framework rewords it.
CANCELLED_TOOL_MARKER = "was cancelled - another message came in"


class Cause(str, Enum):
    """Why the tool call was cancelled. Decides what the model is told."""

    INTERRUPTED_BY_USER = "interrupted_by_user"
    SESSION_ENDED = "session_ended"


_REPLACEMENT = {
    Cause.INTERRUPTED_BY_USER: (
        "INTERRUPTED: this tool call was cancelled because the user sent a new "
        "message while it was running. It did NOT run and nothing it would "
        "have changed was changed. Read the user's new message first: if the "
        "work is still wanted, call the tool again; if the user has moved on, "
        "drop it. Do NOT report it as done."
    ),
    Cause.SESSION_ENDED: (
        "DENIED: the user did not approve this action — the previous session "
        "was interrupted (Ctrl+C, terminal close, crash) before the permission "
        "prompt could be answered. If this action is still required, re-propose "
        "it explicitly so the user can decide. Do NOT assume it succeeded."
    ),
}


def needs_repair(messages: Any) -> bool:
    """Whether *messages* contains a fabricated cancellation result."""
    from langchain_core.messages import ToolMessage

    for m in messages or ():
        if isinstance(m, ToolMessage):
            content = m.content if isinstance(m.content, str) else ""
            if CANCELLED_TOOL_MARKER in content:
                return True
    return False


def rewrite(messages: Any, cause: Cause) -> list[Any]:
    """The replacement ToolMessages for *messages*. Pure; no state access.

    Returns only what changed, which is what ``aupdate_state`` wants and what
    makes this testable against a literal message list — including the exact
    one recovered from the wedged thread.
    """
    from langchain_core.messages import ToolMessage

    body = _REPLACEMENT[cause]
    out: list[Any] = []
    for m in messages or ():
        if not isinstance(m, ToolMessage):
            continue
        content = m.content if isinstance(m.content, str) else ""
        if CANCELLED_TOOL_MARKER not in content:
            continue
        out.append(ToolMessage(
            # Same id: the add_messages reducer merges in place. A new id would
            # append a second result for one call and make things worse.
            id=m.id,
            tool_call_id=m.tool_call_id,
            name=getattr(m, "name", "tool") or "tool",
            status="error",
            content=body,
        ))
    return out


async def repair_orphan_tool_calls(
    agent: Any, thread_id: str, *, cause: Cause = Cause.INTERRUPTED_BY_USER
) -> int:
    """Repair a thread's history in place. Returns how many results changed.

    Never raises: a conversation that cannot be repaired is still better off
    being attempted than being abandoned, and the caller's next step (surface
    the problem to the user) does not depend on the outcome.
    """
    config = {"configurable": {"thread_id": thread_id}}
    try:
        state = await agent.aget_state(config)
    except Exception:
        logger.exception("state read failed for thread=%s", thread_id)
        return 0
    messages = state.values.get("messages", []) if state and state.values else []
    patched = rewrite(messages, cause)
    if not patched:
        return 0
    try:
        await agent.aupdate_state(config, {"messages": patched})
    except Exception:
        logger.exception("state repair failed for thread=%s", thread_id)
        return 0
    logger.info(
        "repaired %d interrupted tool call(s) on thread=%s (%s)",
        len(patched), thread_id, cause.value,
    )
    return len(patched)


__all__ = [
    "CANCELLED_TOOL_MARKER",
    "Cause",
    "needs_repair",
    "repair_orphan_tool_calls",
    "rewrite",
]
