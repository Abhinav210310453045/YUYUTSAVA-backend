"""Streaming + interrupt-handling runtime for compiled deepagent graphs.

The factories in :mod:`yuyutsava.core.engine` build the graphs; this module
drives them. Two execution entrypoints:

  * :func:`astream_agent`       — CLI flow. Prints LLM tokens, tool calls,
                                  tool results to stderr; prompts the user on
                                  stdin when an interrupt fires.
  * :func:`astream_agent_iter`  — Daemon flow. Yields typed :class:`StreamEvent`
                                  records; an ``ask_handler`` callback handles
                                  interrupts (the daemon routes them through
                                  the channel system).

Both share the same interrupt-prompt rendering (:func:`prompt_permission`),
which also persists every interrupt to the audit DB via
:class:`yuyutsava.storage.interrupts.InterruptsStore` when one is supplied.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from yuyutsava.core.text_utils import sanitize_message_metadata
from yuyutsava.ports.agent import Agent
from yuyutsava.core.tool_result import guard_tool_result, is_tool_error
from yuyutsava.core.tracing import get_callback, trace_metadata
from yuyutsava.models.interrupts import (
    PermissionRequestInterrupt,
    TaskRunnerPermissionInterrupt,
    UserQuestionInterrupt,
)
from yuyutsava.storage.interrupts import InterruptsStore
from yuyutsava.storage.models import InterruptRecord

logger = logging.getLogger("yuyutsava")

_SEP = "━" * 60


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------


def flatten_content(content: object) -> str:
    """Flatten LangChain message content (str or block list) to text.

    List content may mix plain-string blocks with {"type":"text"} dicts —
    merge_content appends str chunks to a list-content message as raw strings,
    so both shapes carry prose and must be kept. Dropping the str blocks is
    what truncated the ``final`` event to the first chunk of a reply.
    """
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


def _ai_message_text(msg: AIMessage) -> str:
    return flatten_content(msg.content).strip()


def last_assistant_text(messages: list[Any]) -> str:
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            text = _ai_message_text(m)
            if text:
                return text
    return ""


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())


# ---------------------------------------------------------------------------
# Interrupt rendering helpers
# ---------------------------------------------------------------------------


def _fmt_session_tail(sid: str | None) -> str:
    """Mirror AskCard/ProposalCard rendering: 'sess:…<last 8>'."""
    if not sid:
        return ""
    s = str(sid)
    return f"sess:…{s[-8:]}" if len(s) > 8 else f"sess:{s}"


def _fmt_agent_path(path: str | None) -> str:
    """Mirror UI chip: last two path segments (e.g. cli/general-purpose)."""
    if not path:
        return ""
    parts = [p for p in str(path).split("/") if p]
    return "/".join(parts[-2:]) if parts else ""


def _print_scoping_chips(payload: Any, *, colour: str) -> None:
    """Print agent_path + session tail underneath the interrupt header.

    No-op when neither field is present, so the bare PermissionRequestInterrupt
    branch stays clean if the daemon never populated them.
    """
    if not isinstance(payload, dict):
        return
    ap = _fmt_agent_path(payload.get("agent_path"))
    st = _fmt_session_tail(payload.get("session_id"))
    if not ap and not st:
        return
    chips: list[str] = []
    if ap:
        chips.append(f"agent: {ap}")
    if st:
        chips.append(st)
    print(f"  \033[2m{'   '.join(chips)}\033[0m", file=sys.stderr)


# ---------------------------------------------------------------------------
# Permission prompt / user question handler
# ---------------------------------------------------------------------------

# Accepted yes/no synonyms — also used by the chat REPL's _ask_handler
# (yuyutsava.cli.commands.chat_repl) so both surfaces accept the same
# vocabulary. Anything outside these sets falls through to "reject".
_AFFIRMATIVE: frozenset[str] = frozenset({
    "y", "yes", "a", "approve", "ok", "allow",
})
_NEGATIVE: frozenset[str] = frozenset({
    "n", "no", "r", "reject", "deny", "cancel",
})


def _normalize_yes_no(answer: str) -> str:
    """Map common synonyms to the canonical 'approve' / 'reject' tokens.

    The permission middleware and TaskRunner gateway compare the decision
    string strictly against ``"approve"`` — without normalization the
    user typing ``y`` or ``yes`` would silently reject.
    """
    a = (answer or "").strip().lower()
    if a in _AFFIRMATIVE:
        return "approve"
    return "reject"


async def prompt_permission(
    interrupt_value: Any,
    *,
    interrupts_store: InterruptsStore | None = None,
    session_id: str | None = None,
    thread_id: str | None = None,
    invocation_mode: str = "cli",
) -> str:
    """Render the interrupt payload to the terminal and collect the user's response.

    When ``interrupts_store`` is provided, every interrupt is also persisted
    to the audit DB before prompting and resolved after the user answers.
    Failures to write are swallowed (best-effort audit, never block the user).

    Returns the user's response string:
      • "approve" / "reject"  for permission prompts
      • free-text answer      for user questions
    """
    interrupt_type = interrupt_value.get("type", "") if isinstance(interrupt_value, dict) else ""

    row_id = ""
    if interrupts_store is not None and isinstance(interrupt_value, dict):
        try:
            record = InterruptRecord.from_payload(
                interrupt_value,
                session_id=session_id or "",
                thread_id=thread_id or "",
                invocation_mode=invocation_mode,
            )
            row_id = await interrupts_store.record(record)
        except Exception as exc:  # noqa: BLE001
            logger.warning("InterruptsStore.record raised: %s", exc)

    # ── tr_ask_user: agent asks a free-text question ───────────────────────
    if interrupt_type == "user_question":
        iv = UserQuestionInterrupt.model_validate(interrupt_value)

        print(f"\n\033[36m{_SEP}\033[0m", file=sys.stderr)
        print("\033[36m💬  AGENT QUESTION\033[0m", file=sys.stderr)
        print(f"\033[36m{_SEP}\033[0m", file=sys.stderr)
        print(f"  {iv.question}", file=sys.stderr)
        if iv.options:
            print(f"  Options: {' | '.join(iv.options)}", file=sys.stderr)
        _print_scoping_chips(interrupt_value, colour="36")
        print(f"\033[36m{_SEP}\033[0m", file=sys.stderr)

        answer: str = await asyncio.to_thread(input, "  Your answer: ")
        response = answer.strip() or "no response"
        print(f"\033[36m  → {response}\033[0m\n", file=sys.stderr)
        if interrupts_store is not None and row_id:
            await interrupts_store.resolve(row_id, outcome="answered", user_response=response)
        return response

    # ── TaskRunnerAgent: filesystem operation permission request ───────────
    if interrupt_type == "task_runner_permission":
        iv = TaskRunnerPermissionInterrupt.model_validate(interrupt_value)
        path_str = ", ".join(iv.paths)

        print(f"\n\033[35m{_SEP}\033[0m", file=sys.stderr)
        print("\033[35m🔐  TASK RUNNER PERMISSION REQUEST\033[0m", file=sys.stderr)
        print(f"\033[35m{_SEP}\033[0m", file=sys.stderr)
        print(f"  Operation : {iv.operation.upper()}", file=sys.stderr)
        print(f"  Path(s)   : {path_str}", file=sys.stderr)
        print(f"  Zone      : {iv.zone.upper()}", file=sys.stderr)
        agent_line = f"  Agent     : {iv.requesting_agent}"
        if iv.parent_agent:
            agent_line += f"  (parent: {iv.parent_agent})"
        print(agent_line, file=sys.stderr)
        print(f"  Reason    : {iv.reason}", file=sys.stderr)
        print(f"  Risk      : {iv.risk_level}", file=sys.stderr)
        _print_scoping_chips(interrupt_value, colour="35")
        print(f"\033[35m{_SEP}\033[0m", file=sys.stderr)

        answer = await asyncio.to_thread(input, "  Allow? [y/N]: ")
        decision = _normalize_yes_no(answer)
        if decision == "approve":
            print("\033[32m  ✅  Approved\033[0m\n", file=sys.stderr)
        else:
            print("\033[31m  🚫  Rejected\033[0m\n", file=sys.stderr)
        if interrupts_store is not None and row_id:
            await interrupts_store.resolve(row_id, outcome=decision)
        return decision

    # ── PermissionMiddleware: raw execute call triggered a pattern check ───
    if isinstance(interrupt_value, dict):
        iv2 = PermissionRequestInterrupt.model_validate({
            "command": interrupt_value.get("command", "<unknown>"),
            "reason":  interrupt_value.get("reason", "Potentially dangerous operation"),
        })
    else:
        iv2 = PermissionRequestInterrupt(
            command=str(interrupt_value),
            reason="Potentially dangerous operation",
        )

    print(f"\n\033[33m{_SEP}\033[0m", file=sys.stderr)
    print("\033[33m🛑  PERMISSION REQUEST\033[0m", file=sys.stderr)
    print(f"\033[33m{_SEP}\033[0m", file=sys.stderr)
    print(f"  Command : {iv2.command}", file=sys.stderr)
    print(f"  Reason  : {iv2.reason}", file=sys.stderr)
    _print_scoping_chips(interrupt_value, colour="33")
    print(f"\033[33m{_SEP}\033[0m", file=sys.stderr)

    answer = await asyncio.to_thread(input, "  Allow? [y/N]: ")
    decision = _normalize_yes_no(answer)
    if decision == "approve":
        print("\033[32m  ✅  Approved — running command\033[0m\n", file=sys.stderr)
    else:
        print("\033[31m  🚫  Rejected — command will not run\033[0m\n", file=sys.stderr)
    if interrupts_store is not None and row_id:
        await interrupts_store.resolve(row_id, outcome=decision)
    return decision


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
      - ``log``         data={"text": str}
      - ``final``       data={"text": str}   (last assistant message)
    """

    kind: str
    data: dict


def _image_event_from_result(body: str) -> dict | None:
    """Extract an ``image`` event payload from a ``vis_*`` tool result, or None.

    ``vis_*`` tools return JSON like ``{"status":"ok","visual_id":...,"url":...}``.
    A rendered visual becomes a typed ``image`` frame so the UI can show it inline
    (and any SSE consumer gets structured image data, not opaque text).
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("status") != "ok" or not data.get("visual_id"):
        return None
    return {
        "visual_id": data["visual_id"],
        "url": data.get("url"),
        "kind": data.get("kind"),
        "title": data.get("title"),
        "mime": data.get("mime", "image/png"),
    }


def _artifact_event_from_result(body: str) -> dict | None:
    """Extract an ``artifact`` event payload from an ``artifact_create`` result.

    Turns the tool's record JSON into a typed ``artifact`` frame so the UI can
    render the pluggable block inline (SandboxBlock/AudioBlock/TextBlock/…) and
    open it big — the non-visual twin of :func:`_image_event_from_result`.
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("status") != "ok" or not data.get("artifact_id"):
        return None
    return {
        # attachment_id is the key the frontend block components read; alias it.
        "artifact_id": data["artifact_id"],
        "attachment_id": data["artifact_id"],
        "url": data.get("url"),
        "kind": data.get("kind"),
        "mime": data.get("mime"),
        "title": data.get("title"),
    }


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


def _run_config(
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
            **trace_metadata(session_id=thread_id, trace_name=trace_name,
                             tags=trace_tags),
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


async def _drive_graph(
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
            current_input, config=cfg, stream_mode=["messages", "updates"],
        ):
            if not isinstance(event, tuple) or len(event) != 2:
                continue
            mode, data = event

            if mode == "updates" and isinstance(data, dict) and "__interrupt__" in data:
                for iv in data["__interrupt__"] or []:
                    pending.append((getattr(iv, "id", None),
                                    iv.value if hasattr(iv, "value") else iv))
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


async def astream_agent_iter(
    agent: Agent,
    task: str,
    *,
    thread_id: str | None = None,
    recursion_limit: int = 200,
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
    _tid = thread_id or str(uuid.uuid4())
    cfg = _run_config(
        thread_id=_tid,
        recursion_limit=recursion_limit,
        agent_path=agent_path,
        modality=modality,
        trace_name=run_name,
        trace_tags=["mode:daemon", f"agent:{agent_path}"],
    )

    final_messages: list[Any] = []
    # Explicit stable id: a HumanMessage created with id=None gets a fresh id
    # re-minted by add_messages on every reload of the thread, which breaks the
    # transcript recorder's id-based dedup — the same user turn is written again
    # under a new id each subsequent turn, so the chat history (and the UI that
    # hydrates from it) shows duplicate user bubbles. Pinning the id at creation
    # makes it round-trip through the checkpoint unchanged.
    first_input: Any = {"messages": [HumanMessage(content=task, id=str(uuid.uuid4()))]}
    if resume_value is not None:
        # Answering an interrupt raised by a PREVIOUS process. The graph is
        # still parked at its checkpointed ``interrupt()``, so we re-enter with
        # the decision instead of starting a turn — no new user message, and
        # the agent carries on from exactly where it stopped.
        first_input = Command(resume=resume_value)
    elif resume:
        # Continue this thread from its last committed checkpoint. If the
        # checkpoint is missing or lives in an incompatible backend (e.g. the
        # storage backend was switched on reload), fall back to a fresh run.
        if await _has_resumable_state(agent, cfg):
            first_input = None
        else:
            logger.warning(
                "resume requested for thread %s but no checkpoint state found; "
                "starting a fresh run", _tid,
            )

    steps_last_pass = 0
    async for kind, item in _drive_graph(
        agent, first_input, cfg, ask=ask_handler, collected=final_messages,
    ):
        if kind == "chunk":
            chunk, meta = item
            if not isinstance(chunk, AIMessageChunk):
                continue
            text = flatten_content(chunk.content)
            if not text:
                continue
            # node/ns let renderers tell main-agent prose from LLM calls nested
            # inside the tools node (subagents). ns mirrors langgraph's own
            # namespace derivation: ``langgraph_checkpoint_ns`` minus its last
            # segment, so top-level nodes yield "". Additive keys — consumers
            # that only read "text" are unaffected.
            node = ""
            ns = ""
            if isinstance(meta, dict):
                node = str(meta.get("langgraph_node") or "")
                raw_ns = str(meta.get("langgraph_checkpoint_ns") or "")
                if "|" in raw_ns:
                    ns = raw_ns.rsplit("|", 1)[0]
            yield StreamEvent("token", {"text": text, "node": node, "ns": ns})

        elif kind == "message":
            m = item
            if isinstance(m, AIMessage) and m.tool_calls:
                for tc in m.tool_calls:
                    name = tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
                    args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                    yield StreamEvent("tool_call", {"name": name, "args": args})
            elif isinstance(m, ToolMessage):
                tn = getattr(m, "name", "tool") or "tool"
                body = m.content if isinstance(m.content, str) else str(m.content)
                safe_body = guard_tool_result(body, tn)
                if safe_body is not body:
                    m.content = safe_body
                preview = safe_body if len(safe_body) <= 600 else safe_body[:600] + " …[truncated]"
                payload: dict = {"name": tn, "preview": preview}
                if keep_full_payloads:
                    payload["full"] = safe_body
                yield StreamEvent("tool_result", payload)
                if tn.startswith("vis_"):
                    img = _image_event_from_result(safe_body)
                    if img is not None:
                        yield StreamEvent("image", img)
                elif tn in ("artifact_create", "artifact_show"):
                    art = _artifact_event_from_result(safe_body)
                    if art is not None:
                        yield StreamEvent("artifact", art)

        elif kind == "pass_end":
            steps_last_pass = item

        elif kind == "ask_failed":
            yield StreamEvent("log", {"text": f"ask_handler raised: {item}; rejecting"})

    final_text = last_assistant_text(final_messages)
    if not final_text and steps_last_pass == 0:
        yield StreamEvent("log", {
            "text": (
                f"⚠️  Agent produced no output — possible recursion limit hit "
                f"(limit={recursion_limit}) or graph exited unexpectedly."
            )
        })
    elif not final_text:
        yield StreamEvent("log", {
            "text": "⚠️  Task ended with no assistant text. Agent may have stopped mid-task."
        })
    yield StreamEvent("final", {"text": final_text})


async def astream_agent(
    agent: Agent,
    task: str,
    *,
    thread_id: str | None = None,
    recursion_limit: int = 200,
    on_tick=None,  # async (steps: int) -> None — progress hook for session bookkeeping
    agent_path: str = "cli",
    session_id: str | None = None,
    interrupts_store: InterruptsStore | None = None,
    invocation_mode: str = "cli",
) -> str:
    """Run the agent with real-time async streaming, printing as it goes.

    The terminal sink over :func:`_drive_graph`. Everything about *running* the
    graph — the stream loop, interrupt collection, the resume handshake — is the
    driver's; everything here is presentation:

    - LLM tokens are printed to stderr as they arrive (no buffering).
    - Tool calls and results are logged at INFO with clear labels.
    - ``interrupt()`` pauses and asks the user on stdin, then resumes.
    - Returns the final assistant text.
    - ``on_tick`` (optional): awaited with the count of new messages observed
      after each graph step batch. Used by ``yuyutsava.sessions.runner`` to
      coalesce ``store.touch`` updates without coupling the engine to the
      session layer.
    """
    _tid = thread_id or str(uuid.uuid4())
    cfg = _run_config(
        thread_id=_tid,
        recursion_limit=recursion_limit,
        agent_path=agent_path,
        # The CLI is a typed surface. Seeded explicitly rather than left absent,
        # which is what it was before the drivers were merged — a middleware
        # asking "is this voice?" got no key at all here and the key on the
        # daemon path.
        modality="text",
        trace_name="cli",
        trace_tags=["mode:cli"],
    )

    logger.info(_SEP)
    logger.info("YUYUTSAVA  starting task  thread_id=%s", cfg["configurable"]["thread_id"])
    logger.info(_SEP)
    logger.info("Task: %s", task)
    logger.info(_SEP)

    final_messages: list[Any] = []
    # Explicit stable id so the message round-trips the checkpoint unchanged
    # (see the note in astream_agent_iter — prevents duplicate transcript rows).
    first_input: Any = {"messages": [HumanMessage(content=task, id=str(uuid.uuid4()))]}

    async def _ask(value: Any) -> str:
        return await prompt_permission(
            value,
            interrupts_store=interrupts_store,
            session_id=session_id,
            thread_id=_tid,
            invocation_mode=invocation_mode,
        )

    in_ai_stream = False
    steps_last_pass = 0

    def _close_stream() -> None:
        nonlocal in_ai_stream
        if in_ai_stream:
            print("\n", file=sys.stderr)
            in_ai_stream = False

    async for kind, item in _drive_graph(
        agent, first_input, cfg, ask=_ask, collected=final_messages,
    ):
        if kind == "interrupt_seen":
            _close_stream()

        elif kind == "chunk":
            chunk, _meta = item
            if isinstance(chunk, AIMessageChunk):
                text = flatten_content(chunk.content)
                if text:
                    if not in_ai_stream:
                        print(f"\n\033[36m{'─' * 60}\033[0m", file=sys.stderr)
                        print("\033[36m🤖  AI (streaming)\033[0m", file=sys.stderr)
                        print(f"\033[36m{'─' * 60}\033[0m", file=sys.stderr)
                        in_ai_stream = True
                    print(text, end="", flush=True, file=sys.stderr)
                # Close the AI stream line if a tool call is starting.
                if chunk.tool_calls or getattr(chunk, "tool_call_chunks", None):
                    _close_stream()
            elif isinstance(chunk, ToolMessage):
                _close_stream()

        elif kind == "message":
            _close_stream()
            m = item
            if isinstance(m, AIMessage):
                usage = getattr(m, "usage_metadata", None)
                if usage:
                    parts_u: list[str] = []
                    for k in ("input_tokens", "output_tokens", "total_tokens"):
                        v = usage.get(k) if isinstance(usage, dict) else getattr(usage, k, None)
                        if v is not None:
                            parts_u.append(f"{k.replace('_tokens', '')}: {v}")
                    if parts_u:
                        logger.debug("    Tokens  %s", " | ".join(parts_u))

                if m.tool_calls:
                    for tc in m.tool_calls:
                        name = tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
                        args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                        args_str = json.dumps(args, indent=4) if isinstance(args, dict) else str(args)
                        logger.info("")
                        logger.info("\033[33m🔧  TOOL CALL → %s\033[0m", name)
                        logger.info("    Input:\n%s", _indent(args_str, 4))

            elif isinstance(m, ToolMessage):
                tn = getattr(m, "name", "tool") or "tool"
                body = m.content if isinstance(m.content, str) else str(m.content)
                safe_body = guard_tool_result(body, tn)
                if safe_body is not body:
                    m.content = safe_body
                preview = safe_body if len(safe_body) <= 600 else safe_body[:600] + "\n    … [truncated]"
                err = is_tool_error(m, safe_body)
                logger.info("")
                if err:
                    logger.error("\033[1;31m❌  TOOL ERROR ← %s\033[0m", tn)
                    logger.error("    %s", preview.replace("\n", "\n    "))
                else:
                    logger.info("\033[32m✅  TOOL RESULT ← %s\033[0m", tn)
                    logger.info("    %s", preview.replace("\n", "\n    "))

        elif kind == "pass_end":
            # End of this stream pass — close any dangling AI output line.
            if in_ai_stream:
                print("\n", file=sys.stderr)
            steps_last_pass = item
            # Fire the progress hook once per pass so the runner can coalesce
            # touches. Doing it here (not per-message) keeps the engine ignorant
            # of the store's throttling policy.
            if on_tick is not None and item > 0:
                try:
                    await on_tick(item)
                except Exception:
                    logger.exception("on_tick handler raised; continuing")

    if steps_last_pass == 0 and not final_messages:
        logger.error(
            "⚠️  Agent produced no output — possible recursion limit hit "
            "(limit=%d) or graph exited unexpectedly. "
            "Try re-running with --recursion-limit <higher value>.",
            recursion_limit,
        )

    final_text = last_assistant_text(final_messages)
    if not final_text:
        logger.warning(
            "⚠️  Task ended with no assistant text. The agent may have stopped "
            "mid-task. Check for budget/recursion limit errors above."
        )

    logger.info("")
    logger.info(_SEP)
    logger.info("YUYUTSAVA  task complete")
    logger.info(_SEP)

    return final_text
