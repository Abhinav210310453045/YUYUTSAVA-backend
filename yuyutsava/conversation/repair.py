"""Make a conversation's message history valid again after an interruption.

## The failures this exists for

**13 Sep — a fabricated tool result.** A user sent a chat message while
``tr_run_python`` was in flight. LangGraph cancelled the tool task and wrote a
replacement result::

    ToolMessage(status="success",
                content="Tool call <id> was cancelled - another message came "
                        "in before it could be completed.")

From then on every call returned ``finish_reason: STOP`` with
``output_tokens: 0``, empty content and no tool calls, and the provider's
prompt cache went from 65,882 read tokens to 0 — four consecutive user
messages, ~70,000 input tokens each, and silence.

Two things are wrong with that result and both matter: ``status="success"``
with the word "cancelled" in the body tells a model the call *worked*, which
produces confident hallucinations ("I wrote the file" when nothing was
written); and a result answering a call the model has lost the thread of
leaves a history some providers will not continue from at all.

**14 Sep — an unfinished assistant turn.** The graceful interrupt (Ctrl+S)
cancelled a turn cleanly and appended the user's message, and the thread went
silent anyway, twice, with ``repaired=0``. Reading the transcript back showed
why: there was nothing of the first kind to repair. The tail was

    seq 6400  ai    tool_calls=[tr_grep]        (the model's turn opens)
    seq 6401  tool  status=success              (the tool answers)
    seq 6402  human "no need to study web…"     (the interrupt lands HERE)
    seq 6403  ai    content=''  in=60,813 out=0
    seq 6404  human "get it now?"
    seq 6405  ai    content=''  in=60,978 out=0

The assistant's turn was never **closed**. The model asked for a tool, got its
result, and the next thing in history is a *user* turn — and in Gemini's
content model a ``functionResponse`` is itself a user-role part, so this is two
consecutive user turns with the model's turn left hanging open. Cancelling a
run is not enough: whatever the model was in the middle of has to be concluded
before anything new is appended.

The empty ``AIMessage`` at 6403 then got persisted, so the next turn's history
contained an empty model turn too — a second malformed shape, and a
self-perpetuating one (this repository already carries ``GeminiPartsSafeMixin``
for the 400 an empty ``AIMessage`` causes on Vertex). Honest limit: the thread
recovered on its own two messages later with that same tail still present, so
the shape is strongly *correlated* with the silence rather than proven to be
its sole trigger. It is repaired here because an unclosed turn and a
contentless model turn are wrong on their own terms, and because
:meth:`~yuyutsava.conversation.service.ConversationService._recover_empty_turn`
— which now retries whatever it finds — is what actually rescues the turn.

## What this repairs

:func:`diagnose` reports four things, and :func:`rewrite` fixes each:

* **a fabricated cancellation** — rewritten as an explicit ``status="error"``
  whose body says what happened, keeping ``id`` so LangGraph's ``add_messages``
  reducer merges in place rather than appending a duplicate;
* **a dangling tool call** — a ``tool_call`` with no result *anywhere*, which
  is what a hard cancel leaves when the tool node dies before writing. The
  function was already named ``repair_orphan_tool_calls`` but could not see
  this case at all: it matched only on the marker string;
* **an empty assistant message** — no text, no tool calls. Carries no
  information, and is removed with ``RemoveMessage`` (the reducer's supported
  deletion path) so it cannot accumulate;
* **an unfinished assistant turn** — history ending on a tool result, closed
  with a short assistant message recording the interruption. That message is
  also useful context: it is what lets the model pick the work back up rather
  than guess whether it finished.

The wording differs by cause, because the model should draw different
conclusions. :data:`Cause.INTERRUPTED_BY_USER` — the user interjected, the call
never ran, nothing changed. :data:`Cause.SESSION_ENDED` — a permission prompt
died with the process (Ctrl+C, terminal close, crash), so consent was never
given and the action must be re-proposed rather than assumed.

Previously this lived as a private helper in ``sessions/runner.py`` and ran
**only** on CLI ``--resume``. The daemon — app and voice — never called it,
which is why that first thread stayed wedged forever.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger("yuyutsava.conversation.repair")

#: The text LangGraph puts in the result it fabricates for a cancelled tool
#: task. Matching on it is unavoidable — it is the only marker we get — so it
#: lives here, once, with a test that fails loudly if the framework rewords it.
CANCELLED_TOOL_MARKER = "was cancelled - another message came in"


class Cause(str, Enum):
    """Why the turn was cut short. Decides what the model is told."""

    INTERRUPTED_BY_USER = "interrupted_by_user"
    SESSION_ENDED = "session_ended"


#: What a tool call that never completed is told to report.
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

#: The assistant message that closes an interrupted turn. Short on purpose: it
#: sits in the window for the rest of the conversation.
_TURN_CLOSER = {
    Cause.INTERRUPTED_BY_USER: (
        "[interrupted] The user interrupted this turn before I could finish "
        "reasoning about the tool results above. I have not reported a "
        "conclusion yet."
    ),
    Cause.SESSION_ENDED: (
        "[interrupted] The session ended before I could finish this turn. The "
        "tool results above were collected but never acted on."
    ),
}


def _text_of(msg: Any) -> str:
    """Message text, whether content is a string or a block list.

    Defers to ``core.streaming.flatten_content``, which is the canonical
    flattener — a second implementation here would drift from it and start
    disagreeing about what "empty" means. Imported lazily and defensively:
    ``core.__init__`` pulls in ``engine``, and this module must stay cheap to
    import from the conversation service.
    """
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    try:
        from yuyutsava.core.streaming import flatten_content

        return flatten_content(content) or ""
    except Exception:  # noqa: BLE001 — detection must never raise
        return ""


def _call_field(call: Any, key: str) -> Any:
    return call.get(key) if isinstance(call, dict) else getattr(call, key, None)


@dataclass(frozen=True)
class Findings:
    """What is wrong with a history. Empty means it is safe to send."""

    #: ToolMessages carrying LangGraph's fabricated cancellation text.
    cancelled: tuple[Any, ...] = ()
    #: ``(tool_call_id, name)`` for calls with no result anywhere in history.
    dangling: tuple[tuple[str, str], ...] = ()
    #: Ids of assistant messages with neither text nor a tool call.
    empty_assistant: tuple[str, ...] = ()
    #: Whether the assistant's turn is still open — history ends on a tool
    #: result, so the model never got to say what it concluded.
    unclosed_turn: bool = False

    def count(self, *, close_turn: bool = True) -> int:
        """How many messages a repair would add or change."""
        return (
            len(self.cancelled) + len(self.dangling) + len(self.empty_assistant)
            + (1 if self.unclosed_turn and close_turn else 0)
        )

    def any(self, *, close_turn: bool = True) -> bool:
        return self.count(close_turn=close_turn) > 0


def diagnose(messages: Any) -> Findings:
    """Everything wrong with *messages*, in one read. Never raises."""
    from langchain_core.messages import AIMessage, ToolMessage

    msgs = list(messages or ())
    answered = {
        getattr(m, "tool_call_id", None)
        for m in msgs
        if isinstance(m, ToolMessage)
    }
    cancelled: list[Any] = []
    dangling: list[tuple[str, str]] = []
    empty: list[str] = []

    for m in msgs:
        if isinstance(m, ToolMessage):
            body = m.content if isinstance(m.content, str) else ""
            if CANCELLED_TOOL_MARKER in body:
                cancelled.append(m)
            continue
        if not isinstance(m, AIMessage):
            continue
        calls = list(getattr(m, "tool_calls", None) or ())
        for call in calls:
            cid = _call_field(call, "id")
            if cid and cid not in answered:
                dangling.append((cid, _call_field(call, "name") or "tool"))
        # An assistant message with no text and no call says nothing and is
        # known to make Vertex reject the next request outright.
        if (
            not calls
            and not (getattr(m, "invalid_tool_calls", None) or ())
            and not _text_of(m).strip()
            and getattr(m, "id", None)
        ):
            empty.append(m.id)

    # Is the model's turn still open? Judged on the history that will remain
    # once the empty assistant messages are removed, since those are going.
    doomed = set(empty)
    tail = [
        m for m in msgs
        if not (isinstance(m, AIMessage) and getattr(m, "id", None) in doomed)
    ]
    last = tail[-1] if tail else None
    unclosed = bool(last) and (
        isinstance(last, ToolMessage)
        # A dangling call whose result we are about to synthesise leaves the
        # turn just as open as a real result would.
        or (isinstance(last, AIMessage) and bool(getattr(last, "tool_calls", None) or ()))
    )
    return Findings(tuple(cancelled), tuple(dangling), tuple(empty), unclosed)


def needs_repair(messages: Any, *, close_turn: bool = True) -> bool:
    """Whether *messages* would be sent to a model in a shape it may refuse."""
    return diagnose(messages).any(close_turn=close_turn)


def rewrite(
    messages: Any, cause: Cause, *, close_turn: bool = True
) -> list[Any]:
    """The messages to write back for *messages*. Pure; no state access.

    Returns only what changes, which is what ``aupdate_state`` wants and what
    makes this testable against a literal message list — including the exact
    ones recovered from both wedged threads. Order matters: removals first, so
    the reducer has dropped the empty turns before anything is appended, and
    the closing assistant message last, so the next user message follows a
    model turn.

    *close_turn* is false when the graph is **resuming** a turn rather than
    starting one (answering a permission interrupt): that turn is still the
    model's to finish, and declaring it interrupted would be a lie.
    """
    from langchain_core.messages import AIMessage, RemoveMessage, ToolMessage

    found = diagnose(messages)
    body = _REPLACEMENT[cause]
    out: list[Any] = []

    for m in found.cancelled:
        out.append(ToolMessage(
            # Same id: the add_messages reducer merges in place. A new id would
            # append a second result for one call and make things worse.
            id=m.id,
            tool_call_id=m.tool_call_id,
            name=getattr(m, "name", "tool") or "tool",
            status="error",
            content=body,
        ))
    for mid in found.empty_assistant:
        out.append(RemoveMessage(id=mid))
    for cid, name in found.dangling:
        # No existing message to merge with — the result was never written, so
        # this one is appended and takes a fresh id.
        out.append(ToolMessage(
            tool_call_id=cid, name=name, status="error", content=body,
        ))
    if close_turn and found.unclosed_turn:
        out.append(AIMessage(content=_TURN_CLOSER[cause]))
    return out


async def repair_orphan_tool_calls(
    agent: Any,
    thread_id: str,
    *,
    cause: Cause = Cause.INTERRUPTED_BY_USER,
    close_turn: bool = True,
) -> int:
    """Repair a thread's history in place. Returns how many messages changed.

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
    patched = rewrite(messages, cause, close_turn=close_turn)
    if not patched:
        return 0
    try:
        await agent.aupdate_state(config, {"messages": patched})
    except Exception:
        logger.exception("state repair failed for thread=%s", thread_id)
        return 0
    found = diagnose(messages)
    logger.info(
        "repaired thread=%s (%s): %d fabricated, %d dangling, %d empty, "
        "turn closed=%s",
        thread_id, cause.value, len(found.cancelled), len(found.dangling),
        len(found.empty_assistant), bool(close_turn and found.unclosed_turn),
    )
    return len(patched)


__all__ = [
    "CANCELLED_TOOL_MARKER",
    "Cause",
    "Findings",
    "diagnose",
    "needs_repair",
    "repair_orphan_tool_calls",
    "rewrite",
]
