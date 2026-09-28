"""What a turn does about a history the model will not continue from.

Two moments, one sequence (read the checkpoint, :func:`~.repair.diagnose`,
repair). **Before every turn** (:func:`repair_before_turn`), so no message is
sent into a history in a shape the model refuses; and **after a turn came back
empty** (:func:`recover_empty_turn`), where the turn is retried — first on the
same history, then with a hidden nudge — and the user is always told what
happened.

:mod:`~yuyutsava.conversation.repair` rewrites the checkpoint; this module
decides when to, and what the user hears about it. Nothing here raises into
a turn — the user is waiting on both paths.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage

from yuyutsava.conversation.repair import (
    Cause,
    diagnose,
    internal_kwargs,
    repair_orphan_tool_calls,
)
from yuyutsava.core.streaming import StreamEvent

logger = logging.getLogger("yuyutsava.conversation.recovery")

# An async sink for stream events; sync callables are also accepted and called
# directly (the result is awaited only when it's awaitable).
EventSink = Callable[[StreamEvent], Awaitable[None] | None]
#: One pass of the graph: ``(input, resume_value) -> (final_text, steps)``. The
#: input is the user's text, a prepared message, or ``None`` to continue from the
#: checkpoint as it stands.
Drive = Callable[[str | BaseMessage | None, object | None], Awaitable[tuple[str, int]]]

#: The graph node the retry writes its repair as, so the model runs next. Pinned
#: by ``test/framework_contract/test_retry_node_contract.py``.
TOOLS_NODE = "tools"

#: What the second retry says. It changes the input, which is what breaks a
#: near-deterministic model out of repeating the same empty think; it is marked
#: internal, so no history view shows it.
_NUDGE = (
    "(Automatic retry: your previous reply was empty.) Reply to my last message now, "
    "using the tool results above if there are any. If nothing is left to do, say so briefly."
)


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


#: Finish reasons meaning "the model hit its output cap", as the SDKs spell them
#: (Gemini ``MAX_TOKENS``, OpenAI-style ``length``), after normalising case and
#: any ``FinishReason.`` enum prefix.
_OUT_OF_ROOM = frozenset({"MAX_TOKENS", "LENGTH", "MAX_OUTPUT_TOKENS"})


@dataclass(frozen=True)
class EmptyReply:
    """What the model reported about a reply that carried no text and no call."""

    finish_reason: str
    input_tokens: int
    output_tokens: int
    thinking_tokens: int

    @property
    def ran_out_of_room(self) -> bool:
        """The model used its whole output cap — typically all of it thinking."""
        return self.finish_reason in _OUT_OF_ROOM

    def describe(self) -> str:
        reason = self.finish_reason or "unknown"
        return (
            f"finish_reason={reason}, {self.input_tokens:,} in, "
            f"{self.output_tokens:,} out, {self.thinking_tokens:,} thinking"
        )


def _token(usage: Any, *path: str) -> int:
    for key in path:
        usage = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        if usage is None:
            return 0
    try:
        return int(usage)
    except (TypeError, ValueError):
        return 0


def _normalise_reason(raw: str) -> str:
    """``MAX_TOKENS`` from any SDK spelling.

    A streamed reply's metadata merges the chunks' reasons by concatenation, so
    Vertex reports ``FINISH_REASON_UNSPECIFIEDFINISH_REASON_UNSPECIFIEDMAX_TOKENS``;
    an enum renders as ``FinishReason.MAX_TOKENS``.
    """
    return raw.rsplit(".", 1)[-1].upper().replace("FINISH_REASON_UNSPECIFIED", "")


def describe_empty_reply(messages: Any) -> EmptyReply | None:
    """What the latest empty reply in *messages* reported, or ``None`` if there is none.

    Read before the repair removes the message: it is the only record of why
    the model said nothing (``finish_reason`` is logged nowhere else, and the
    usage row counts visible output only).
    """
    empty = set(diagnose(messages).empty_assistant)
    reply = next(
        (m for m in reversed(list(messages or ())) if getattr(m, "id", None) in empty),
        None,
    )
    if reply is None:
        return None
    raw = str((getattr(reply, "response_metadata", None) or {}).get("finish_reason") or "")
    usage = getattr(reply, "usage_metadata", None) or {}
    return EmptyReply(
        finish_reason=_normalise_reason(raw),
        input_tokens=_token(usage, "input_tokens"),
        output_tokens=_token(usage, "output_tokens"),
        thinking_tokens=_token(usage, "output_token_details", "reasoning"),
    )


async def _checkpoint_messages(agent: Any, thread_id: str) -> list[Any]:
    """The thread's checkpointed messages; empty when they cannot be read."""
    try:
        state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
    except Exception:  # noqa: BLE001 — diagnostics must not block the recovery
        logger.debug("checkpoint read for diagnostics failed", exc_info=True)
        return []
    return list(state.values.get("messages", [])) if state and state.values else []


async def inspect_and_repair(
    agent: Any,
    thread_id: str,
    *,
    close_turn: bool = True,
    cause: Cause = Cause.INTERRUPTED_BY_USER,
    as_node: str | None = None,
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
        if not found.any(close_turn=close_turn) and as_node is None:
            return found, 0
        repaired = await repair_orphan_tool_calls(
            agent,
            thread_id,
            cause=cause,
            close_turn=close_turn,
            as_node=as_node,
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

    The failure this exists for: a thinking model spends its whole output cap
    thinking over a large tool result and stops with no text and no call — and,
    at a low temperature, does the same again on the same history.

    An escalating ladder, each rung only when the one before came back empty:

    1. **Re-run the model** on the same history: the empty reply is removed and
       the repair is written as the tools node, so the graph's next step is the
       model. No message is added — the user's is already in the history, and
       sending it twice would make the model answer itself. Handles the
       transient case.
    2. **Nudge**: one hidden instruction to reply now. A changed input is what
       breaks a near-deterministic model out of repeating itself.
    3. **Give up visibly**: close the turn with an honest note to the model and
       tell the user, who can start a new chat or try again.

    The user hears about it at every stage — silence is indistinguishable from
    a hang, and it is the one outcome a conversational agent must never produce.
    """
    steps = 0
    facts = describe_empty_reply(await _checkpoint_messages(agent, thread_id))
    if facts is not None:
        logger.warning("empty reply on thread=%s: %s", thread_id, facts.describe())
    found, _ = await inspect_and_repair(
        agent,
        thread_id,
        close_turn=False,
        cause=Cause.EMPTY_REPLY,
        as_node=TOOLS_NODE,
        what="empty-turn repair",
    )
    await notice(on_event, "warning", _retrying_text(facts, found))

    nudge = _nudge()
    for rung, retry_input in (("re-run", None), ("nudge", nudge)):
        try:
            final, taken = await drive(retry_input, None)
            steps += taken
        except Exception:  # noqa: BLE001 — a failed rung falls through to the next, then to the notice
            logger.exception("empty-turn retry (%s) failed on thread=%s", rung, thread_id)
            final = ""
        if final:
            return final, steps
        if rung == "re-run":
            # The re-run came back empty too: clear that reply before the nudge.
            await inspect_and_repair(
                agent,
                thread_id,
                close_turn=False,
                cause=Cause.EMPTY_REPLY,
                what="empty-turn repair",
            )

    # Giving up: take the nudge back out — an unanswered "reply now" would sit in
    # front of the user's next message — then close the turn honestly.
    await _remove(agent, thread_id, nudge.id)
    await inspect_and_repair(
        agent, thread_id, cause=Cause.EMPTY_REPLY, what="closing an empty turn"
    )
    await notice(on_event, "error", _gave_up_text(facts))
    logger.warning(
        "empty turn on thread=%s (re-run and nudge both empty) — surfaced to the user",
        thread_id,
    )
    return "", steps


async def _remove(agent: Any, thread_id: str, message_id: str | None) -> None:
    """Remove one message from the checkpoint; a failure only costs a stray note."""
    try:
        await agent.aupdate_state(
            {"configurable": {"thread_id": thread_id}},
            {"messages": [RemoveMessage(id=message_id)]},
        )
    except Exception:  # noqa: BLE001 — the give-up notice must still go out
        logger.debug("removing the retry nudge failed", exc_info=True)


def _nudge() -> HumanMessage:
    return HumanMessage(
        content=_NUDGE, id=str(uuid.uuid4()), additional_kwargs=internal_kwargs("retry_nudge")
    )


def _retrying_text(facts: EmptyReply | None, found: Any) -> str:
    why = (
        "The model used its whole output budget thinking and returned nothing"
        if facts is not None and facts.ran_out_of_room
        else "The model returned an empty reply"
    )
    extra = [
        part
        for part, n in (
            ("cancelled tool call", len(getattr(found, "cancelled", ()))),
            ("tool call with no result", len(getattr(found, "dangling", ()))),
        )
        if n
    ]
    repaired = f" (also repaired: {', '.join(extra)})" if extra else ""
    return f"{why}{repaired}. Retrying."


def _gave_up_text(facts: EmptyReply | None) -> str:
    why = (
        "it kept using its whole output budget thinking"
        if facts is not None and facts.ran_out_of_room
        else "it kept replying with nothing — no text and no tool call"
    )
    return (
        f"The model could not answer: {why}, on a re-run and after a nudge. "
        "Your message is still in this conversation. Start a new chat, or try again."
    )


async def notice(on_event: EventSink, level: str, text: str) -> None:
    """Tell the user something the stream itself did not say."""
    await emit(on_event, StreamEvent("notice", {"level": level, "text": text}))


async def emit(on_event: EventSink, ev: StreamEvent) -> None:
    """Forward one synthesised event, tolerating a sink that raises."""
    try:
        result = on_event(ev)
        if result is not None:
            await result
    except Exception:  # noqa: BLE001 — a broken event sink must not stop the notice path
        logger.debug("event sink raised on a synthesised notice", exc_info=True)
