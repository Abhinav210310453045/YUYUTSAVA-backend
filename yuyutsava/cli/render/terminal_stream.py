"""What a person sees when a run happens in their terminal.

:func:`~yuyutsava.core.streaming.drive_graph` runs the graph and reports what
happened; this is the sink that turns that into terminal output. LLM tokens go
to stderr as they arrive, unbuffered, so a long answer reads as it is written;
each tool call and result is logged with a label; and when the graph raises an
``interrupt()``, :func:`prompt_permission` renders the request and reads the
answer from stdin.

The daemon consumes the same driver through typed ``StreamEvent``s, which is why
none of this belongs beside it: ANSI colour, ``print`` and ``input()`` are a
front end's business, and keeping them in ``core/`` is what left the agent
builder importing ``sys`` and owning 34 ``print`` calls (R-PLC-03).

``prompt_permission`` also persists every interrupt to the audit store when one
is supplied — what was asked and what the user answered, kept past the life of
the terminal that asked it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)

from yuyutsava.ansi import BOLD_RED, CYAN, DIM, GREEN, MAGENTA, RED, RESET, YELLOW
from yuyutsava.core.streaming import drive_graph, last_assistant_text, run_config
from yuyutsava.core.tool_result import guard_tool_result, is_tool_error
from yuyutsava.models.interrupts import (
    PermissionRequestInterrupt,
    TaskRunnerPermissionInterrupt,
    UserQuestionInterrupt,
)
from yuyutsava.ports.agent import Agent
from yuyutsava.storage.interrupts import InterruptsStore
from yuyutsava.storage.models import InterruptRecord
from yuyutsava.text import flatten_content

# Still the "yuyutsava" logger, not a module-named child: these records are the
# CLI's user-facing output, formatted by the handler setup_logging installs on
# that name. Renaming it would change what the user sees, which a move may not.
logger = logging.getLogger("yuyutsava")


_SEP = "━" * 60


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())


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
    print(f"  {DIM}{'   '.join(chips)}{RESET}", file=sys.stderr)


# Accepted yes/no synonyms — also used by the chat REPL's _ask_handler
# (yuyutsava.cli.commands.chat_repl) so both surfaces accept the same
# vocabulary. Anything outside these sets falls through to "reject".
_AFFIRMATIVE: frozenset[str] = frozenset(
    {
        "y",
        "yes",
        "a",
        "approve",
        "ok",
        "allow",
    }
)

_NEGATIVE: frozenset[str] = frozenset(
    {
        "n",
        "no",
        "r",
        "reject",
        "deny",
        "cancel",
    }
)


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


async def _record_interrupt(
    interrupt_value: Any,
    interrupts_store: InterruptsStore | None,
    *,
    session_id: str | None,
    thread_id: str | None,
    invocation_mode: str,
) -> str:
    """Write the interrupt to the audit DB before asking; its row id, or ``""``.

    Best-effort: a failed write is logged and never blocks the user.
    """
    if interrupts_store is None or not isinstance(interrupt_value, dict):
        return ""
    try:
        record = InterruptRecord.from_payload(
            interrupt_value,
            session_id=session_id or "",
            thread_id=thread_id or "",
            invocation_mode=invocation_mode,
        )
        return await interrupts_store.record(record)
    except Exception as exc:  # noqa: BLE001 — the audit row must not cost the prompt
        logger.warning("InterruptsStore.record raised: %s", exc)
        return ""


async def _ask_question(interrupt_value: dict) -> tuple[str, str]:
    """tr_ask_user: a free-text question. ``(answer, audit outcome)``."""
    iv = UserQuestionInterrupt.model_validate(interrupt_value)
    print(f"\n{CYAN}{_SEP}{RESET}", file=sys.stderr)
    print(f"{CYAN}💬  AGENT QUESTION{RESET}", file=sys.stderr)
    print(f"{CYAN}{_SEP}{RESET}", file=sys.stderr)
    print(f"  {iv.question}", file=sys.stderr)
    if iv.options:
        print(f"  Options: {' | '.join(iv.options)}", file=sys.stderr)
    _print_scoping_chips(interrupt_value, colour="36")
    print(f"{CYAN}{_SEP}{RESET}", file=sys.stderr)

    answer: str = await asyncio.to_thread(input, "  Your answer: ")
    response = answer.strip() or "no response"
    print(f"{CYAN}  → {response}{RESET}\n", file=sys.stderr)
    return response, "answered"


async def _ask_task_runner(interrupt_value: dict) -> tuple[str, str]:
    """TaskRunnerAgent: a filesystem operation needs approval."""
    iv = TaskRunnerPermissionInterrupt.model_validate(interrupt_value)
    print(f"\n{MAGENTA}{_SEP}{RESET}", file=sys.stderr)
    print(f"{MAGENTA}🔐  TASK RUNNER PERMISSION REQUEST{RESET}", file=sys.stderr)
    print(f"{MAGENTA}{_SEP}{RESET}", file=sys.stderr)
    print(f"  Operation : {iv.operation.upper()}", file=sys.stderr)
    print(f"  Path(s)   : {', '.join(iv.paths)}", file=sys.stderr)
    print(f"  Zone      : {iv.zone.upper()}", file=sys.stderr)
    agent_line = f"  Agent     : {iv.requesting_agent}"
    if iv.parent_agent:
        agent_line += f"  (parent: {iv.parent_agent})"
    print(agent_line, file=sys.stderr)
    print(f"  Reason    : {iv.reason}", file=sys.stderr)
    print(f"  Risk      : {iv.risk_level}", file=sys.stderr)
    _print_scoping_chips(interrupt_value, colour="35")
    print(f"{MAGENTA}{_SEP}{RESET}", file=sys.stderr)

    decision = _normalize_yes_no(await asyncio.to_thread(input, "  Allow? [y/N]: "))
    if decision == "approve":
        print(f"{GREEN}  ✅  Approved{RESET}\n", file=sys.stderr)
    else:
        print(f"{RED}  🚫  Rejected{RESET}\n", file=sys.stderr)
    return decision, decision


async def _ask_command(interrupt_value: Any) -> tuple[str, str]:
    """PermissionPolicy: a raw execute call triggered a pattern check."""
    if isinstance(interrupt_value, dict):
        iv = PermissionRequestInterrupt.model_validate(
            {
                "command": interrupt_value.get("command", "<unknown>"),
                "reason": interrupt_value.get("reason", "Potentially dangerous operation"),
            }
        )
    else:
        iv = PermissionRequestInterrupt(
            command=str(interrupt_value),
            reason="Potentially dangerous operation",
        )
    print(f"\n{YELLOW}{_SEP}{RESET}", file=sys.stderr)
    print(f"{YELLOW}🛑  PERMISSION REQUEST{RESET}", file=sys.stderr)
    print(f"{YELLOW}{_SEP}{RESET}", file=sys.stderr)
    print(f"  Command : {iv.command}", file=sys.stderr)
    print(f"  Reason  : {iv.reason}", file=sys.stderr)
    _print_scoping_chips(interrupt_value, colour="33")
    print(f"{YELLOW}{_SEP}{RESET}", file=sys.stderr)

    decision = _normalize_yes_no(await asyncio.to_thread(input, "  Allow? [y/N]: "))
    if decision == "approve":
        print(f"{GREEN}  ✅  Approved — running command{RESET}\n", file=sys.stderr)
    else:
        print(f"{RED}  🚫  Rejected — command will not run{RESET}\n", file=sys.stderr)
    return decision, decision


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
    row_id = await _record_interrupt(
        interrupt_value,
        interrupts_store,
        session_id=session_id,
        thread_id=thread_id,
        invocation_mode=invocation_mode,
    )
    if interrupt_type == "user_question":
        response, outcome = await _ask_question(interrupt_value)
    elif interrupt_type == "task_runner_permission":
        response, outcome = await _ask_task_runner(interrupt_value)
    else:
        response, outcome = await _ask_command(interrupt_value)
    if interrupts_store is not None and row_id:
        if outcome == "answered":
            await interrupts_store.resolve(row_id, outcome=outcome, user_response=response)
        else:
            await interrupts_store.resolve(row_id, outcome=outcome)
    return response


class _AiStreamLine:
    """The streamed-AI block on stderr: opened by the first token, closed by
    anything else the turn prints."""

    def __init__(self) -> None:
        self.open = False

    def write(self, text: str) -> None:
        if not self.open:
            print(f"\n{CYAN}{'─' * 60}{RESET}", file=sys.stderr)
            print(f"{CYAN}🤖  AI (streaming){RESET}", file=sys.stderr)
            print(f"{CYAN}{'─' * 60}{RESET}", file=sys.stderr)
            self.open = True
        print(text, end="", flush=True, file=sys.stderr)

    def close(self) -> None:
        if self.open:
            print("\n", file=sys.stderr)
            self.open = False


def _on_chunk(line: _AiStreamLine, chunk: Any) -> None:
    """Stream an AI chunk's text; a tool call or tool message ends the line."""
    if isinstance(chunk, AIMessageChunk):
        text = flatten_content(chunk.content)
        if text:
            line.write(text)
        # Close the AI stream line if a tool call is starting.
        if chunk.tool_calls or getattr(chunk, "tool_call_chunks", None):
            line.close()
    elif isinstance(chunk, ToolMessage):
        line.close()


def _log_ai_message(m: AIMessage) -> None:
    """Token usage at DEBUG, then each tool call with its arguments."""
    usage = getattr(m, "usage_metadata", None)
    if usage:
        parts_u: list[str] = []
        for k in ("input_tokens", "output_tokens", "total_tokens"):
            v = usage.get(k) if isinstance(usage, dict) else getattr(usage, k, None)
            if v is not None:
                parts_u.append(f"{k.replace('_tokens', '')}: {v}")
        if parts_u:
            logger.debug("    Tokens  %s", " | ".join(parts_u))
    for tc in m.tool_calls or ():
        name = tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
        args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
        args_str = json.dumps(args, indent=4) if isinstance(args, dict) else str(args)
        logger.info("")
        logger.info(f"{YELLOW}🔧  TOOL CALL → %s{RESET}", name)
        logger.info("    Input:\n%s", _indent(args_str, 4))


def _log_tool_message(m: ToolMessage) -> None:
    """The tool result, guarded for size (the message is rewritten too) and
    previewed; logged as an error when the tool reported one."""
    tn = getattr(m, "name", "tool") or "tool"
    body = m.content if isinstance(m.content, str) else str(m.content)
    safe_body = guard_tool_result(body, tn)
    if safe_body is not body:
        m.content = safe_body
    preview = safe_body if len(safe_body) <= 600 else safe_body[:600] + "\n    … [truncated]"
    logger.info("")
    if is_tool_error(m, safe_body):
        logger.error(f"{BOLD_RED}❌  TOOL ERROR ← %s{RESET}", tn)
        logger.error("    %s", preview.replace("\n", "\n    "))
    else:
        logger.info(f"{GREEN}✅  TOOL RESULT ← %s{RESET}", tn)
        logger.info("    %s", preview.replace("\n", "\n    "))


async def astream_agent(
    agent: Agent,
    task: str | BaseMessage | None,
    *,
    thread_id: str | None = None,
    recursion_limit: int = 1000,
    on_tick=None,  # async (steps: int) -> None — progress hook for session bookkeeping
    agent_path: str = "cli",
    session_id: str | None = None,
    interrupts_store: InterruptsStore | None = None,
    invocation_mode: str = "cli",
) -> str:
    """Run the agent with real-time async streaming, printing as it goes.

    The terminal sink over :func:`drive_graph`. Everything about *running* the
    graph — the stream loop, interrupt collection, the resume handshake — is the
    driver's; everything here is presentation:

    - LLM tokens are printed to stderr as they arrive (no buffering).
    - Tool calls and results are logged at INFO with clear labels.
    - ``interrupt()`` pauses and asks the user on stdin, then resumes.
    - Returns the final assistant text.
    - ``task`` of ``None`` sends no new message: the graph continues from its
      checkpoint (the empty-turn retry, whose user message is already there).
    - ``on_tick`` (optional): awaited with the count of new messages observed
      after each graph step batch. Used by ``yuyutsava.cli.session_run`` to
      coalesce ``store.touch`` updates without coupling the engine to the
      session layer.
    """
    _tid = thread_id or str(uuid.uuid4())
    cfg = run_config(
        thread_id=_tid,
        recursion_limit=recursion_limit,
        agent_path=agent_path,
        # The CLI is a typed surface, and says so: a middleware asking "is this
        # voice?" gets the same key on every path.
        modality="text",
        trace_name="cli",
        trace_tags=["mode:cli"],
    )

    logger.info(_SEP)
    logger.info("YUYUTSAVA  starting task  thread_id=%s", cfg["configurable"]["thread_id"])
    logger.info(_SEP)
    logger.info(
        "Task: %s",
        "(continuing from the checkpoint)"
        if task is None
        else ("(an internal retry message)" if isinstance(task, BaseMessage) else task),
    )
    logger.info(_SEP)

    final_messages: list[Any] = []
    # Explicit stable id so the message round-trips the checkpoint unchanged
    # (see the note in core.streaming — prevents duplicate transcript rows).
    if task is None:
        first_input: Any = None
    elif isinstance(task, BaseMessage):
        first_input = {"messages": [task]}
    else:
        first_input = {"messages": [HumanMessage(content=task, id=str(uuid.uuid4()))]}

    async def _ask(value: Any) -> str:
        return await prompt_permission(
            value,
            interrupts_store=interrupts_store,
            session_id=session_id,
            thread_id=_tid,
            invocation_mode=invocation_mode,
        )

    line = _AiStreamLine()
    steps_last_pass = 0
    async for kind, item in drive_graph(
        agent,
        first_input,
        cfg,
        ask=_ask,
        collected=final_messages,
    ):
        if kind == "interrupt_seen":
            line.close()
        elif kind == "chunk":
            _on_chunk(line, item[0])
        elif kind == "message":
            line.close()
            if isinstance(item, AIMessage):
                _log_ai_message(item)
            elif isinstance(item, ToolMessage):
                _log_tool_message(item)
        elif kind == "pass_end":
            # A pass that ends mid-line ended without an interrupt, so it is the
            # last one: nothing streams after it.
            line.close()
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
