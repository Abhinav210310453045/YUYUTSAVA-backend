"""What a turn does about a history the model will not continue from.

Two moments, one sequence (read the checkpoint, :func:`~.repair.diagnose`,
repair). **Before every turn** (:func:`repair_before_turn`), so no message is
sent into a history in a shape the model refuses; and **after a turn came back
empty** (:func:`recover_empty_turn`), where the turn is retried once and the user
is always told what happened.

:mod:`~yuyutsava.conversation.repair` rewrites the checkpoint; this module
decides when to, and what the user hears about it. Nothing here raises into
a turn — the user is waiting on both paths.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from yuyutsava.conversation.repair import Cause, diagnose, repair_orphan_tool_calls
from yuyutsava.core.streaming import StreamEvent

logger = logging.getLogger("yuyutsava.conversation.recovery")

# An async sink for stream events; sync callables are also accepted and called
# directly (the result is awaited only when it's awaitable).
EventSink = Callable[[StreamEvent], Awaitable[None] | None]
#: One pass of the graph: ``(user_text, resume_value) -> (final_text, steps)``.
Drive = Callable[[str | None, object | None], Awaitable[tuple[str, int]]]


def repair_summary(found: Any, *, close_turn: bool = True) -> str:
    """Plain-English list of what a repair changed, for a user-facing notice.

    "1 tool call was cancelled" was the only story the notice could tell, so a
    repair that closed an unfinished turn or dropped an empty reply announced
    itself as something it was not.
    """
    parts: list[str] = []
    n = len(getattr(found, "cancelled", ()))
    if n:
        parts.append(f"{n} cancelled tool call{'s' if n != 1 else ''}")
    n = len(getattr(found, "dangling", ()))
    if n:
        parts.append(f"{n} tool call{'s' if n != 1 else ''} with no result")
    n = len(getattr(found, "empty_assistant", ()))
    if n:
        parts.append(f"{n} empty repl{'ies' if n != 1 else 'y'}")
    if close_turn and getattr(found, "unclosed_turn", False):
        parts.append("an unfinished turn")
    if not parts:
        return "nothing to repair"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


async def inspect_and_repair(
    agent: Any,
    thread_id: str,
    *,
    close_turn: bool = True,
    what: str,
) -> tuple[Any | None, int]:
    """Read *thread_id*'s checkpoint, diagnose it, and repair if needed.

    Returns ``(findings, repaired)``. ``findings`` is ``None`` only when
    the inspection itself failed, which is why the two are returned
    together: a caller cannot tell "nothing was wrong" from "we could not
    look" any other way, and they lead to different notices.

    Never raises. Both callers reach this on a path where the user is
    waiting — before a turn, or after one came back empty — and a failed
    inspection must not block the turn or swallow the reply.
    """
    try:
        state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
        messages = state.values.get("messages", []) if state and state.values else []
        found = diagnose(messages)
        if not found.any(close_turn=close_turn):
            return found, 0
        repaired = await repair_orphan_tool_calls(
            agent,
            thread_id,
            cause=Cause.INTERRUPTED_BY_USER,
            close_turn=close_turn,
        )
        return found, repaired
    except Exception:  # noqa: BLE001 — see the docstring
        logger.debug("%s failed", what, exc_info=True)
        return None, 0


async def repair_before_turn(
    agent: Any,
    thread_id: str,
    on_event: EventSink,
    *,
    close_turn: bool = True,
) -> None:
    """Repair an interrupted tool call before sending anything new.

    Costs one checkpoint read per turn, which is nothing beside a model
    call, and buys an unconditional guarantee: a conversation cannot be
    sent into the model in a shape it refuses to continue from. The
    alternative — repairing only where a front happens to know it just
    cancelled something — is how the app path ended up with no repair at
    all while the CLI resume path had one.
    """
    found, repaired = await inspect_and_repair(
        agent, thread_id, close_turn=close_turn, what="pre-turn repair"
    )
    if repaired:
        await notice(
            on_event,
            "info",
            "Picking up after an interruption — "
            + repair_summary(found, close_turn=close_turn)
            + ".",
        )


async def recover_empty_turn(
    agent: Any,
    thread_id: str,
    on_event: EventSink,
    drive: Drive,
) -> tuple[str, int]:
    """A turn produced nothing. Try to fix it, and never stay silent.

    The failure this exists for: after a fabricated "cancelled" tool result,
    every call returned ``finish_reason: STOP`` with zero output tokens —
    each turn cost ~70,000 input tokens and said nothing.

    Two steps, in order:

    1. Repair whatever the history is carrying (see
       :mod:`yuyutsava.conversation.repair`) and drive the graph **once**
       more with no new user message, so the agent answers the message
       already in its history.
    2. Whatever happens, emit a ``notice`` the user can see. Silence is
       indistinguishable from a hang, and it is the one outcome a
       conversational agent must never produce.

    The retry is unconditional — an empty turn with nothing to repair is
    often transient, and the user's next message goes through — and it is
    exactly one, so an unrecoverable thread cannot spend in a loop.
    """
    steps = 0
    found, repaired = await inspect_and_repair(agent, thread_id, what="empty-turn inspection")
    if repaired and found is not None:
        await notice(
            on_event,
            "warning",
            "The previous turn left the history in a shape the model "
            "would not continue from (" + repair_summary(found) + "). Repaired — retrying.",
        )
    else:
        await notice(
            on_event,
            "warning",
            "The model returned nothing. Retrying once — an empty response is often transient.",
        )
    # No new user message: theirs is already in the history, and sending it
    # twice would make the model answer itself.
    try:
        final, steps = await drive(None, None)
    except Exception:  # noqa: BLE001
        logger.exception("retry after an empty turn failed")
        final = ""
    if final:
        return final, steps

    await notice(
        on_event,
        "error",
        "The model returned an empty response twice — no text and no "
        "tool call, before and after a repair. The conversation's "
        "history is in a state it will not continue from. Try /new for "
        "a fresh session; your last message is still in the history, "
        "so nothing you wrote is lost.",
    )
    logger.warning(
        "empty turn on thread=%s (repaired=%d, retried=1) — surfaced to the user",
        thread_id,
        repaired,
    )
    return "", steps


async def notice(on_event: EventSink, level: str, text: str) -> None:
    """Tell the user something the stream itself did not say."""
    await emit(on_event, StreamEvent("notice", {"level": level, "text": text}))


async def emit(on_event: EventSink, ev: StreamEvent) -> None:
    """Forward one synthesised event, tolerating a sink that raises."""
    try:
        result = on_event(ev)
        if result is not None:
            await result
    except Exception:  # noqa: BLE001
        logger.debug("event sink raised on a synthesised notice", exc_info=True)
