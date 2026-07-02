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
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from yuyutsava.core.text_utils import sanitize_message_metadata
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


def _ai_message_text(msg: AIMessage) -> str:
    c = msg.content
    if isinstance(c, str) and c.strip():
        return c.strip()
    if isinstance(c, list):
        parts: list[str] = []
        for block in c:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts).strip()
    return ""


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


async def _has_resumable_state(agent: CompiledStateGraph, cfg: RunnableConfig) -> bool:
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


async def astream_agent_iter(
    agent: CompiledStateGraph,
    task: str,
    *,
    thread_id: str | None = None,
    recursion_limit: int = 200,
    ask_handler=None,  # async (interrupt_value: dict) -> str
    run_name: str = "agent",
    agent_path: str = "orchestrator",
    keep_full_payloads: bool = False,
    resume: bool = False,
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
    channel — e.g. ``VoiceStyleMiddleware`` makes voice replies short and
    spoken. The same agent graph serves both; only the per-turn value differs.

    ``keep_full_payloads``: when True, tool_result payloads include a
    ``full`` field with the untruncated body alongside the 600-char
    ``preview``. The chat REPL passes True so its ``/expand`` slash command
    can show full output. Default False keeps daemon SSE frames small.

    ``resume``: durable resume after a daemon reload. When True (and a
    checkpoint exists for ``thread_id``), the graph continues from its last
    committed checkpoint instead of starting a new turn — ``task`` is only
    used as the fresh-run fallback when no resumable state is found.

    Yields events; the final yielded event is always ``StreamEvent("final", {"text": ...})``.
    """
    _tid = thread_id or str(uuid.uuid4())
    cfg: RunnableConfig = {
        "recursion_limit": recursion_limit,
        "configurable": {"thread_id": _tid, "agent_path": agent_path, "modality": modality},
    }
    _lf_cb = get_callback()
    if _lf_cb is not None:
        cfg["callbacks"] = [_lf_cb]
        cfg["metadata"] = {
            **(cfg.get("metadata") or {}),
            **trace_metadata(
                session_id=_tid,
                trace_name=run_name,
                tags=["mode:daemon", f"agent:{agent_path}"],
            ),
        }

    final_messages: list[Any] = []
    current_input: Any = {"messages": [HumanMessage(content=task)]}
    if resume:
        # Continue this thread from its last committed checkpoint. If the
        # checkpoint is missing or lives in an incompatible backend (e.g. the
        # storage backend was switched on reload), fall back to a fresh run.
        if await _has_resumable_state(agent, cfg):
            current_input = None
        else:
            logger.warning(
                "resume requested for thread %s but no checkpoint state found; "
                "starting a fresh run", _tid,
            )

    while True:
        # pending: every interrupt fired in this pass, in arrival order.
        # LangGraph requires Command(resume={id: value, ...}) whenever >1
        # interrupt is pending — see fix note in the file docstring.
        pending: list[tuple[str | None, Any]] = []
        _steps_this_pass = 0

        async for event in agent.astream(
            current_input, config=cfg, stream_mode=["messages", "updates"],
        ):
            if not isinstance(event, tuple) or len(event) != 2:
                continue
            mode, data = event

            if mode == "updates" and isinstance(data, dict) and "__interrupt__" in data:
                interrupts = data["__interrupt__"]
                for iv in interrupts or []:
                    it_id = getattr(iv, "id", None)
                    value = iv.value if hasattr(iv, "value") else iv
                    pending.append((it_id, value))
                continue

            if mode == "messages":
                chunk, _meta = data
                if isinstance(chunk, AIMessageChunk):
                    text = ""
                    if isinstance(chunk.content, str):
                        text = chunk.content
                    elif isinstance(chunk.content, list):
                        for block in chunk.content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text += str(block.get("text", ""))
                    if text:
                        yield StreamEvent("token", {"text": text})

            elif mode == "updates":
                if not isinstance(data, dict):
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
                        final_messages.append(m)
                        _steps_this_pass += 1
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

        if not pending:
            final_text = last_assistant_text(final_messages)
            if not final_text and _steps_this_pass == 0:
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
            return

        # Ask the user for every pending interrupt, then resume the graph
        # with a single Command. Use resume_map form when N>1 — required
        # by LangGraph; keep scalar form for N==1 to minimize diff risk
        # for any node that may not accept the map form.
        decisions: list[tuple[str | None, str]] = []
        for it_id, value in pending:
            if ask_handler is None:
                decision = "reject"
            else:
                try:
                    decision = await ask_handler(value)
                except Exception as exc:
                    yield StreamEvent("log", {"text": f"ask_handler raised: {exc}; rejecting"})
                    decision = "reject"
            decisions.append((it_id, decision))

        if len(decisions) == 1:
            current_input = Command(resume=decisions[0][1])
        elif all(it_id is None for it_id, _ in decisions):
            # No ids at all (older LangGraph / single resumable task): the map
            # form can't route, so fall back to a scalar resume with the first
            # decision rather than dropping every answer and effectively rejecting.
            current_input = Command(resume=decisions[0][1])
        else:
            resume_map: dict[str, Any] = {}
            for it_id, decision in decisions:
                if it_id is None:
                    # Mixed id/no-id is unexpected; skipping would silently reject
                    # this op, so leave it out of the map and let LangGraph re-emit
                    # the interrupt on the next pass (re-prompt, not auto-reject).
                    continue
                resume_map[it_id] = decision
            current_input = Command(resume=resume_map)


async def astream_agent(
    agent: CompiledStateGraph,
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
    """
    Run the agent with real-time async streaming.

    - LLM tokens are printed to stderr as they arrive (no buffering).
    - Tool calls and results are logged at INFO level with clear labels.
    - Handles ``interrupt()`` events from ``PermissionMiddleware``: pauses,
      asks the user on stdin, then resumes the graph with approve/reject.
    - Returns the final assistant text.
    - ``on_tick`` (optional): awaited with the count of new messages observed
      after each graph step batch. Used by ``yuyutsava.sessions.runner`` to
      coalesce ``store.touch`` updates without coupling the engine to the
      session layer.
    """
    _tid = thread_id or str(uuid.uuid4())
    cfg: RunnableConfig = {
        "recursion_limit": recursion_limit,
        "configurable": {"thread_id": _tid, "agent_path": agent_path},
    }
    _lf_cb = get_callback()
    if _lf_cb is not None:
        cfg["callbacks"] = [_lf_cb]
        cfg["metadata"] = {
            **(cfg.get("metadata") or {}),
            **trace_metadata(session_id=_tid, trace_name="cli", tags=["mode:cli"]),
        }

    logger.info(_SEP)
    logger.info("YUYUTSAVA  starting task  thread_id=%s", cfg["configurable"]["thread_id"])
    logger.info(_SEP)
    logger.info("Task: %s", task)
    logger.info(_SEP)

    final_messages: list[Any] = []
    # First call uses the task message; subsequent calls (after interrupt) use Command(resume=...)
    current_input: Any = {"messages": [HumanMessage(content=task)]}

    while True:
        _in_ai_stream = False
        # See note in astream_agent_iter — same multi-interrupt handling.
        pending: list[tuple[str | None, Any]] = []
        _steps_this_pass = 0

        # We stream with two modes at once:
        #   "messages" → yields (mode, (chunk, metadata)) — LLM tokens as they arrive
        #   "updates"  → yields (mode, {"node": state_delta}) — tool calls / results
        async for event in agent.astream(
            current_input,
            config=cfg,
            stream_mode=["messages", "updates"],
        ):
            # With multiple stream_mode values, events are (mode, data) tuples
            if not isinstance(event, tuple) or len(event) != 2:
                continue
            mode, data = event

            # ── interrupt detection (updates mode) ─────────────────────────
            if mode == "updates" and isinstance(data, dict) and "__interrupt__" in data:
                if _in_ai_stream:
                    print("\n", file=sys.stderr)
                    _in_ai_stream = False
                interrupts = data["__interrupt__"]
                for iv in interrupts or []:
                    it_id = getattr(iv, "id", None)
                    value = iv.value if hasattr(iv, "value") else iv
                    pending.append((it_id, value))
                continue  # let any other events in this batch process normally

            # ── messages mode: streaming LLM tokens ────────────────────────
            if mode == "messages":
                chunk, _meta = data
                if isinstance(chunk, AIMessageChunk):
                    text = ""
                    if isinstance(chunk.content, str):
                        text = chunk.content
                    elif isinstance(chunk.content, list):
                        for block in chunk.content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text += str(block.get("text", ""))

                    if text:
                        if not _in_ai_stream:
                            print(f"\n\033[36m{'─' * 60}\033[0m", file=sys.stderr)
                            print("\033[36m🤖  AI (streaming)\033[0m", file=sys.stderr)
                            print(f"\033[36m{'─' * 60}\033[0m", file=sys.stderr)
                            _in_ai_stream = True
                        print(text, end="", flush=True, file=sys.stderr)

                    # Close the AI stream line if a tool call is starting
                    if chunk.tool_calls or getattr(chunk, "tool_call_chunks", None):
                        if _in_ai_stream:
                            print("\n", file=sys.stderr)
                            _in_ai_stream = False

                elif isinstance(chunk, ToolMessage):
                    if _in_ai_stream:
                        print("\n", file=sys.stderr)
                        _in_ai_stream = False

            # ── updates mode: full state delta after each node ──────────────
            elif mode == "updates":
                if _in_ai_stream:
                    print("\n", file=sys.stderr)
                    _in_ai_stream = False

                if not isinstance(data, dict):
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
                        final_messages.append(m)
                        _steps_this_pass += 1

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

        # End of this stream pass — close any dangling AI output line
        if _in_ai_stream:
            print("\n", file=sys.stderr)

        # Fire the progress hook once per pass so the runner can coalesce
        # touches. Doing it here (not per-message) keeps the engine ignorant
        # of the store's throttling policy.
        if on_tick is not None and _steps_this_pass > 0:
            try:
                await on_tick(_steps_this_pass)
            except Exception:
                logger.exception("on_tick handler raised; continuing")

        # No interrupt → done
        if not pending:
            if _steps_this_pass == 0 and not final_messages:
                logger.error(
                    "⚠️  Agent produced no output — possible recursion limit hit "
                    "(limit=%d) or graph exited unexpectedly. "
                    "Try re-running with --recursion-limit <higher value>.",
                    recursion_limit,
                )
            break

        # Ask the user for every pending interrupt, then resume the graph
        # with a single Command. resume_map form is required by LangGraph
        # when more than one interrupt is in flight; keep the scalar form
        # for the common single-interrupt case.
        decisions: list[tuple[str | None, str]] = []
        for it_id, value in pending:
            decision = await prompt_permission(
                value,
                interrupts_store=interrupts_store,
                session_id=session_id,
                thread_id=_tid,
                invocation_mode=invocation_mode,
            )
            decisions.append((it_id, decision))

        if len(decisions) == 1:
            current_input = Command(resume=decisions[0][1])
        else:
            resume_map: dict[str, Any] = {}
            for it_id, decision in decisions:
                if it_id is None:
                    continue
                resume_map[it_id] = decision
            current_input = Command(resume=resume_map)

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
