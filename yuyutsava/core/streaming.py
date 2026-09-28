"""Running a compiled deepagent graph, and reporting what happened.

The factories in :mod:`yuyutsava.core.engine` build the graphs; this module runs
them. :func:`drive_graph` is the one driver — it owns the stream loop, collects
interrupts and implements the resume handshake — and a *sink* decides what those
events become:

  * :func:`astream_agent_iter`, here — yields typed :class:`StreamEvent` records
    for the daemon, which answers interrupts through the channel system with the
    ``ask_handler`` it passes in.
  * :func:`yuyutsava.cli.render.terminal_stream.astream_agent` — the terminal
    surface: tokens on stderr, the interrupt prompt on stdin. It lives in
    ``cli/`` because ``print`` and ``input()`` belong to a front end, not to the
    runtime every front end shares.

Because both sinks call the same driver, the resume protocol exists once
(ADR-004 item 5) instead of drifting between two copies.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from yuyutsava.artifacts.events import ARTIFACT_EVENT
from yuyutsava.core.tool_result import guard_tool_result
from yuyutsava.llm.tracing import get_callback, trace_metadata
from yuyutsava.ports.agent import Agent
from yuyutsava.ports.tool_events import ToolEventRegistry
from yuyutsava.text import flatten_content, sanitize_message_metadata
from yuyutsava.visuals.events import IMAGE_EVENT

logger = logging.getLogger("yuyutsava")

#: What a tool result can turn into besides text. Each package owns the rule for
#: its own results; the driver holds the list and knows nothing about ``vis_*``
#: or ``artifact_create`` beyond the order it tries them in.
_TOOL_EVENTS = ToolEventRegistry((IMAGE_EVENT, ARTIFACT_EVENT))


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------


def _ai_message_text(msg: AIMessage) -> str:
    return flatten_content(msg.content).strip()


def last_assistant_text(messages: list[Any]) -> str:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            text = _ai_message_text(m)
            if text:
                return text
    return ""


# ---------------------------------------------------------------------------
# Typed stream events  (consumed by the daemon's channel router)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamEvent:
    """Structured event yielded by ``astream_agent_iter``.

    ``kind``:
      - ``token``       data={"text": str}
      - ``tool_call``   data={"name": str, "args": dict}
      - ``tool_result`` data={"name": str, "preview": str}
      - ``image``       data={"visual_id","url","kind","title","mime"}
      - ``artifact``    data={"artifact_id","attachment_id","url","kind","mime","title"}
      - ``log``         data={"text": str}
      - ``notice``      data={"text": str, "level": "info"|"warning"|"error"}
      - ``final``       data={"text": str}   (last assistant message)

    ``log`` versus ``notice``: a renderer may drop a ``log`` when it has
    nowhere to put it (the app attaches them to the streaming bubble, and a
    turn that streamed nothing has no bubble). A ``notice`` may never be
    dropped — it is how "the model returned an empty response" reaches the
    user, and that must not look like a hang. A turn that produced no prose
    swallowed four of these before the distinction existed.
    """

    kind: str
    data: dict


async def _has_resumable_state(agent: Agent, cfg: RunnableConfig) -> bool:
    """True when ``cfg``'s thread has a checkpoint with messages to resume.

    Used by :func:`astream_agent_iter` to decide between continuing an
    interrupted run (``input=None``) and a fresh run. Never raises — a missing
    or unreadable checkpoint just means "not resumable".
    """
    try:
        snap = await agent.aget_state(cfg)
    except Exception:
        logger.exception("aget_state failed while checking for resumable state")
        return False
    values = getattr(snap, "values", None)
    if not isinstance(values, dict):
        return False
    return bool(values.get("messages"))


def run_config(
    *,
    thread_id: str,
    recursion_limit: int,
    agent_path: str,
    modality: str | None = None,
    trace_name: str,
    trace_tags: list[str],
) -> RunnableConfig:
    """The per-run config, built once for both entrypoints.

    The two drivers built this separately and disagreed about it: only the
    daemon one seeded ``modality``, so a middleware asking "is this a voice
    turn?" always got "no" on the CLI path.
    """
    configurable: dict[str, Any] = {"thread_id": thread_id, "agent_path": agent_path}
    if modality is not None:
        configurable["modality"] = modality
    cfg: RunnableConfig = {
        "recursion_limit": recursion_limit,
        "configurable": configurable,
    }
    callback = get_callback()
    if callback is not None:
        cfg["callbacks"] = [callback]
        cfg["metadata"] = {
            **(cfg.get("metadata") or {}),
            **trace_metadata(session_id=thread_id, trace_name=trace_name, tags=trace_tags),
        }
    return cfg


def _resume_command(decisions: list[tuple[str | None, str]]) -> Command:
    """Build the ``Command`` that re-enters a graph parked on interrupt(s).

    **One copy of the resume protocol.** It was written twice — once per driver
    — and the copies drifted: the daemon's handles the case where several
    interrupts arrive with no ids, and the CLI's built ``Command(resume={})``
    there, discarding every answer the user had just given. That is `F-D03`'s
    "the resume protocol is hand-implemented twice" with a concrete bill.

    LangGraph requires the keyed form once more than one interrupt is pending;
    the scalar form is kept for the single case to avoid relying on map support
    in any node that may not accept it.
    """
    if len(decisions) == 1:
        return Command(resume=decisions[0][1])
    if all(it_id is None for it_id, _ in decisions):
        # No ids at all (older LangGraph / a single resumable task): the map
        # form cannot route, so fall back to a scalar resume with the first
        # decision rather than dropping every answer and effectively rejecting.
        return Command(resume=decisions[0][1])
    resume_map: dict[str, Any] = {}
    for it_id, decision in decisions:
        if it_id is None:
            # Mixed id/no-id is unexpected; skipping would silently reject this
            # op, so leave it out of the map and let LangGraph re-emit the
            # interrupt on the next pass (re-prompt, not auto-reject).
            continue
        resume_map[it_id] = decision
    return Command(resume=resume_map)


async def drive_graph(
    agent: Agent,
    first_input: Any,
    cfg: RunnableConfig,
    *,
    ask: Any,
    collected: list[Any],
):
    """The graph loop, once. Decodes the stream and owns the resume protocol.

    ADR-004 item 5: *"collapse ``astream_agent`` / ``astream_agent_iter`` into
    one driver plus a sink"*. This is the driver. The two entrypoints are the
    sinks — one turns these items into ``StreamEvent``s, the other into stderr
    and log lines — and neither re-implements the loop, the interrupt
    collection, or the resume handshake.

    Deliberately low-level: it yields what the graph said, not a presentation
    vocabulary. Forcing one event shape on both consumers would have meant
    bending the CLI's byte-for-byte output to fit the daemon's payloads, and the
    CLI output is a user-facing surface.

    Yields:
        ``("chunk", (chunk, meta))``  every ``messages``-mode item
        ``("message", m)``           each message in an ``updates`` delta,
                                     already sanitized and appended to
                                     *collected*
        ``("pass_end", steps)``      end of a stream pass; *steps* is how many
                                     messages it produced

    *ask* is awaited once per pending interrupt with the interrupt's value and
    must return a decision string. ``None`` auto-rejects, which is what a
    headless run wants.
    """
    current_input: Any = first_input

    while True:
        pending: list[tuple[str | None, Any]] = []
        steps = 0

        async for event in agent.astream(
            current_input,
            config=cfg,
            stream_mode=["messages", "updates"],
        ):
            if not isinstance(event, tuple) or len(event) != 2:
                continue
            mode, data = event

            if mode == "updates" and isinstance(data, dict) and "__interrupt__" in data:
                for iv in data["__interrupt__"] or []:
                    pending.append(
                        (getattr(iv, "id", None), iv.value if hasattr(iv, "value") else iv)
                    )
                yield ("interrupt_seen", None)
                continue

            if mode == "messages":
                yield ("chunk", data)
                continue

            if mode != "updates" or not isinstance(data, dict):
                continue
            for _node_name, node_data in data.items():
                if not isinstance(node_data, dict):
                    continue
                msgs = node_data.get("messages", [])
                if not isinstance(msgs, list):
                    continue
                for m in msgs:
                    if isinstance(m, AIMessage):
                        sanitize_message_metadata(m)
                    collected.append(m)
                    steps += 1
                    yield ("message", m)

        yield ("pass_end", steps)

        if not pending:
            return

        decisions: list[tuple[str | None, str]] = []
        for it_id, value in pending:
            if ask is None:
                decision = "reject"
            else:
                try:
                    decision = await ask(value)
                except Exception as exc:  # noqa: BLE001 — a failed ask must not end the run
                    yield ("ask_failed", exc)
                    decision = "reject"
            decisions.append((it_id, decision))

        current_input = _resume_command(decisions)


async def _first_input(
    agent: Agent,
    cfg: RunnableConfig,
    task: str | BaseMessage | None,
    *,
    resume: bool,
    resume_value: Any | None,
) -> Any:
    """What the graph is entered with: a new turn, a resume, or an old answer.

    ``task`` is the user's text, a message already built (the empty-turn retry's
    hidden nudge), or ``None`` — continue from the checkpoint as it stands, which
    the retry has positioned so the model runs next.
    """
    if resume_value is not None:
        # Answering an interrupt raised by a PREVIOUS process. The graph is
        # still parked at its checkpointed ``interrupt()``, so we re-enter with
        # the decision instead of starting a turn — no new user message, and
        # the agent carries on from exactly where it stopped.
        return Command(resume=resume_value)
    if resume:
        # Continue this thread from its last committed checkpoint. If the
        # checkpoint is missing or lives in an incompatible backend (e.g. the
        # storage backend was switched on reload), fall back to a fresh run.
        if await _has_resumable_state(agent, cfg):
            return None
        logger.warning(
            "resume requested for thread %s but no checkpoint state found; starting a fresh run",
            cfg["configurable"]["thread_id"],
        )
    if task is None:
        return None
    if isinstance(task, BaseMessage):
        return {"messages": [task]}
    # Explicit stable id: a HumanMessage created with id=None gets a fresh id
    # re-minted by add_messages on every reload of the thread, which breaks the
    # transcript recorder's id-based dedup — the same user turn is written again
    # under a new id each subsequent turn, so the chat history (and the UI that
    # hydrates from it) shows duplicate user bubbles. Pinning the id at creation
    # makes it round-trip through the checkpoint unchanged.
    return {"messages": [HumanMessage(content=task, id=str(uuid.uuid4()))]}


def _token_event(chunk: Any, meta: Any) -> StreamEvent | None:
    """A streamed AI chunk as a ``token`` event, or ``None`` if it carries no text.

    node/ns let renderers tell main-agent prose from LLM calls nested inside the
    tools node (subagents). ns mirrors langgraph's own namespace derivation:
    ``langgraph_checkpoint_ns`` minus its last segment, so top-level nodes yield
    "". Additive keys — consumers that only read "text" are unaffected.
    """
    if not isinstance(chunk, AIMessageChunk):
        return None
    text = flatten_content(chunk.content)
    if not text:
        return None
    node = ""
    ns = ""
    if isinstance(meta, dict):
        node = str(meta.get("langgraph_node") or "")
        raw_ns = str(meta.get("langgraph_checkpoint_ns") or "")
        if "|" in raw_ns:
            ns = raw_ns.rsplit("|", 1)[0]
    return StreamEvent("token", {"text": text, "node": node, "ns": ns})


def _message_events(m: Any, *, keep_full_payloads: bool) -> list[StreamEvent]:
    """The events one completed message produces: tool calls, or a tool result.

    A tool result is guarded against size before anything sees it (the message
    itself is rewritten, so the transcript keeps the guarded body too), then
    previewed; the owning package's rule may add an ``image``/``artifact`` event.
    """
    if isinstance(m, AIMessage) and m.tool_calls:
        events = []
        for tc in m.tool_calls:
            name = tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            events.append(StreamEvent("tool_call", {"name": name, "args": args}))
        return events
    if not isinstance(m, ToolMessage):
        return []
    tn = getattr(m, "name", "tool") or "tool"
    body = m.content if isinstance(m.content, str) else str(m.content)
    safe_body = guard_tool_result(body, tn)
    if safe_body is not body:
        m.content = safe_body
    preview = safe_body if len(safe_body) <= 600 else safe_body[:600] + " …[truncated]"
    payload: dict = {"name": tn, "preview": preview}
    if keep_full_payloads:
        payload["full"] = safe_body
    events = [StreamEvent("tool_result", payload)]
    extra = _TOOL_EVENTS.event_for(tn, safe_body)
    if extra is not None:
        events.append(StreamEvent(*extra))
    return events


def _empty_turn_notice(
    final_text: str, steps_last_pass: int, recursion_limit: int
) -> StreamEvent | None:
    """A notice when a turn ends without assistant text; ``None`` otherwise.

    These are notices, not logs. As logs the app dropped them — it attaches a
    log to the streaming bubble, and a turn that streamed nothing has no bubble
    — so consecutive dead turns reached the user as silence.
    """
    if final_text:
        return None
    if steps_last_pass == 0:
        return StreamEvent(
            "notice",
            {
                "level": "error",
                "text": (
                    f"Agent produced no output — possible recursion limit hit "
                    f"(limit={recursion_limit}) or graph exited unexpectedly."
                ),
            },
        )
    return StreamEvent(
        "notice",
        {
            "level": "warning",
            "text": "Task ended with no assistant text. The agent may have stopped mid-task.",
        },
    )


async def astream_agent_iter(
    agent: Agent,
    task: str | BaseMessage | None,
    *,
    thread_id: str | None = None,
    recursion_limit: int = 1000,
    ask_handler=None,  # async (interrupt_value: dict) -> str
    run_name: str = "agent",
    agent_path: str = "orchestrator",
    keep_full_payloads: bool = False,
    resume: bool = False,
    resume_value: Any | None = None,
    modality: str = "text",
):
    """Async generator that yields ``StreamEvent``s instead of printing them.

    The daemon uses this to feed events into the channel router. ``ask_handler``
    is called whenever the graph emits an ``interrupt`` — the daemon plugs in
    a function that routes through the web/terminal channel and returns the
    user's decision string. If ``ask_handler`` is None, interrupts are
    auto-rejected (suitable for headless / autonomous runs).

    ``agent_path`` is seeded into ``configurable`` so downstream interrupts can
    attribute themselves (``"orchestrator"`` by default — daemon-style;
    ``"cli"`` for direct CLI runs).

    ``modality`` is seeded into ``configurable`` (``"text"`` by default,
    ``"voice"`` for spoken turns) so a middleware can style the reply for the
    channel — e.g. ``VoiceStylePolicy`` makes voice replies short and
    spoken. The same agent graph serves both; only the per-turn value differs.

    ``keep_full_payloads``: when True, tool_result payloads include a
    ``full`` field with the untruncated body alongside the 600-char
    ``preview``. The chat REPL passes True so its ``/expand`` slash command
    can show full output. Default False keeps daemon SSE frames small.

    ``resume``: durable resume after a daemon reload. When True (and a
    checkpoint exists for ``thread_id``), the graph continues from its last
    committed checkpoint instead of starting a new turn — ``task`` is only
    used as the fresh-run fallback when no resumable state is found.

    ``resume_value``: answer an interrupt raised by an earlier process. Takes
    precedence over ``resume``/``task``: the graph is re-entered with
    ``Command(resume=<value>)``, which is how a HITL ask answered *after* a
    daemon restart still reaches the agent that was waiting for it. Pass a
    plain decision string for a single interrupt, or ``{interrupt_id: decision}``
    when the turn was blocked on several.

    Yields events; the final yielded event is always ``StreamEvent("final", {"text": ...})``.
    """
    cfg = run_config(
        thread_id=thread_id or str(uuid.uuid4()),
        recursion_limit=recursion_limit,
        agent_path=agent_path,
        modality=modality,
        trace_name=run_name,
        trace_tags=["mode:daemon", f"agent:{agent_path}"],
    )
    first_input = await _first_input(agent, cfg, task, resume=resume, resume_value=resume_value)

    final_messages: list[Any] = []
    steps_last_pass = 0
    async for kind, item in drive_graph(
        agent,
        first_input,
        cfg,
        ask=ask_handler,
        collected=final_messages,
    ):
        if kind == "chunk":
            event = _token_event(*item)
            if event is not None:
                yield event
        elif kind == "message":
            for event in _message_events(item, keep_full_payloads=keep_full_payloads):
                yield event
        elif kind == "pass_end":
            steps_last_pass = item
        elif kind == "ask_failed":
            yield StreamEvent("log", {"text": f"ask_handler raised: {item}; rejecting"})

    final_text = last_assistant_text(final_messages)
    notice = _empty_turn_notice(final_text, steps_last_pass, recursion_limit)
    if notice is not None:
        yield notice
    yield StreamEvent("final", {"text": final_text})
