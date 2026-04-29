"""
Build a **Deep Agents** graph with an OpenAI-compatible chat model and a real-disk backend.

Uses ``LocalShellBackend`` (filesystem + ``execute``) so built-in ``read_file`` /
``write_file`` / ``ls`` / … map to the workspace root, and ``execute`` runs shell
on the host per deepagents (see ``deepagents.backends.LocalShellBackend``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from yuyutsava.core.config import DockerSettings, LocalSettings, LlmSettings
from yuyutsava.core.docker_sandbox_backend import DockerSandboxBackend
from yuyutsava.core.llm import chat_model
from yuyutsava.core.permission_middleware import PermissionMiddleware
from yuyutsava.core.tool_filter_middleware import ToolFilterMiddleware
from yuyutsava.agents.task_runner.tools import bind_tools as _bind_task_runner_tools
from yuyutsava.agents.task_runner.prompts import task_runner_rules_section
from yuyutsava.models.interrupts import (
    PermissionRequestInterrupt,
    TaskRunnerPermissionInterrupt,
    UserQuestionInterrupt,
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger("yuyutsava")


def setup_logging(verbose: bool = False) -> None:
    """Configure root yuyutsava logger to print coloured lines to stderr."""
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)

    class _Fmt(logging.Formatter):
        _COLOURS = {
            logging.DEBUG:    "\033[2m",       # dim
            logging.INFO:     "\033[0m",        # normal
            logging.WARNING:  "\033[33m",       # yellow
            logging.ERROR:    "\033[31m",       # red
            logging.CRITICAL: "\033[1;31m",     # bold red
        }
        _RESET = "\033[0m"

        def format(self, record: logging.LogRecord) -> str:
            colour = self._COLOURS.get(record.levelno, "")
            msg = super().format(record)
            return f"{colour}{msg}{self._RESET}"

    handler.setFormatter(_Fmt("%(message)s"))
    log = logging.getLogger("yuyutsava")
    log.setLevel(level)
    log.handlers.clear()
    log.addHandler(handler)
    log.propagate = False


# ---------------------------------------------------------------------------
# Agent bundle
# ---------------------------------------------------------------------------


@dataclass
class AgentBundle:
    """Compiled Deep Agent graph plus optional Docker resources to tear down."""

    agent: CompiledStateGraph
    docker_backend: DockerSandboxBackend | None = None
    sandbox_root: Path | None = None
    output_dir: Path | None = None

    def close(self) -> None:
        if self.docker_backend is not None:
            self.docker_backend.stop()


def builtin_tools_reference_json() -> str:
    """Static reference for ``yuyutsava --print-tools`` (names match Deep Agents middleware)."""
    doc = [
        {
            "tool": "read_file",
            "note": "Read text/binary via FilesystemBackend; use virtual paths under workspace (e.g. /yuyutsava/foo.txt).",
        },
        {
            "tool": "write_file",
            "note": "Create/overwrite files under the same virtual root.",
        },
        {
            "tool": "execute",
            "note": "Shell on the local machine (LocalShellBackend); prefer over ad-hoc bash wrappers. Timeout matches CLI --bash-timeout.",
        },
        {
            "tool": "ls, glob, grep, edit_file, write_todos, task, …",
            "note": "Other built-in Deep Agents tools; see https://docs.langchain.com/oss/python/deepagents/overview",
        },
    ]
    return json.dumps(doc, indent=2)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------


def _local_system_prompt(
    workspace_root: Path,
    sandbox_root: Path | None = None,
    output_dir: Path | None = None,
) -> str:
    root = workspace_root.resolve()
    sb = sandbox_root.resolve() if sandbox_root is not None else root / "_sandbox"
    out = output_dir.resolve() if output_dir is not None else root / "_output"
    return f"""\
{task_runner_rules_section(root, sb, out)}

## WORKSPACE CONTEXT

Root: {root} | Mode: real disk + local shell.
Output dir: {out} — write all deliverables here, not to the sandbox.

PATH RULES (critical):
- ls and glob use VIRTUAL paths: `/` is the workspace root, NOT the real filesystem root.
  - To list workspace root: ls(path="/")
  - To list a subdirectory: ls(path="/subdir")
  - NEVER pass a real absolute path like `{root}` to ls or glob — it will return empty.
- ls and glob RETURN virtual paths. Before passing them to tr_* tools, convert to real paths:
  virtual `/foo.xlsx`  →  real `{root}/foo.xlsx`
  virtual `/subdir/bar.py`  →  real `{root}/subdir/bar.py`
- Never pass a virtual path directly to tr_read_file / tr_write_file / tr_delete_file.

Complete the user's task; be concise."""


def _docker_system_prompt(workspace_root: Path, export_host: Path | None) -> str:
    root = workspace_root.resolve()
    sandbox = root / "_sandbox"
    extra = ""
    if export_host is not None:
        exp = export_host.resolve()
        extra = f" Output dir: host {exp} → /output in container (write deliverables to /output/...)."
    return f"""\
{task_runner_rules_section(root, sandbox)}

## WORKSPACE CONTEXT

Mode: Docker sandbox (isolated from host shell).
Mount: host {root} → /workspace.{extra}

PATH RULES (critical):
- ls and glob use VIRTUAL paths: `/` is the workspace root (/workspace), NOT the real filesystem root.
  - To list workspace root: ls(path="/")
  - To list a subdirectory: ls(path="/subdir")
  - NEVER pass a real absolute path like `/workspace` to ls or glob — it will return empty.
- ls and glob RETURN virtual paths. Before passing them to tr_* tools, convert to real paths:
  virtual `/foo.xlsx`  →  real `/workspace/foo.xlsx`
  virtual `/subdir/bar.py`  →  real `/workspace/subdir/bar.py`
- Never pass a virtual path directly to tr_read_file / tr_write_file / tr_delete_file.

Complete the user's task; be concise."""


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------


def _local_shell_backend_factory(workspace_root: Path, bash_timeout_sec: int):
    root = str(workspace_root.resolve())

    def factory(_runtime: Any) -> LocalShellBackend:
        return LocalShellBackend(
            root_dir=root,
            virtual_mode=True,
            timeout=bash_timeout_sec,
            inherit_env=True,
        )

    return factory


# ---------------------------------------------------------------------------
# Build agent
# ---------------------------------------------------------------------------


def build_agent(
    workspace_root: Path,
    settings: LlmSettings,
    *,
    bash_timeout_sec: int = 120,
    execution_mode: Literal["local", "docker"] = "local",
    docker_settings: DockerSettings | None = None,
    local_settings: LocalSettings | None = None,
    permission_check: bool = True,
) -> AgentBundle:
    """Build a Deep Agent; ``local`` uses ``LocalShellBackend``, ``docker`` uses ``DockerSandboxBackend``.

    Args:
        permission_check: When ``True`` (default), attaches ``PermissionMiddleware`` so
            the agent pauses and asks the user before running dangerous shell commands.
            Pass ``False`` to disable (e.g. in automated / non-interactive pipelines).
    """
    model = chat_model(settings)
    checkpointer = MemorySaver()
    middleware = [ToolFilterMiddleware()]
    if permission_check:
        middleware.append(PermissionMiddleware(workspace_root=workspace_root.resolve()))

    loc = local_settings or LocalSettings()
    ws = workspace_root.resolve()
    sandbox_root = (loc.sandbox_dir.resolve() if loc.sandbox_dir else ws / "_sandbox")
    output_dir   = (loc.output_dir.resolve()  if loc.output_dir  else ws / "_output")

    if execution_mode == "docker":
        docker_cfg = docker_settings or DockerSettings()
        export = docker_cfg.export_dir.resolve() if docker_cfg.export_dir else None
        docker_backend = DockerSandboxBackend(
            image=docker_cfg.image,
            workspace_host=ws,
            export_host=export,
            network=docker_cfg.network,
            timeout=bash_timeout_sec,
            memory=docker_cfg.memory,
            cpus=docker_cfg.cpus,
            pids_limit=docker_cfg.pids_limit,
        )
        graph = create_deep_agent(
            model=model,
            tools=_bind_task_runner_tools(ws),
            backend=docker_backend,
            system_prompt=_docker_system_prompt(workspace_root, docker_cfg.export_dir),
            checkpointer=checkpointer,
            middleware=middleware,
            debug=False,
        )
        return AgentBundle(agent=graph, docker_backend=docker_backend)

    backend = _local_shell_backend_factory(workspace_root, bash_timeout_sec)
    graph = create_deep_agent(
        model=model,
        tools=_bind_task_runner_tools(ws, sandbox_root),
        backend=backend,
        system_prompt=_local_system_prompt(workspace_root, sandbox_root, output_dir),
        checkpointer=checkpointer,
        middleware=middleware,
        debug=False,
    )
    return AgentBundle(agent=graph, docker_backend=None, sandbox_root=sandbox_root, output_dir=output_dir)


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


_STATE_GRAPH_PNG_PATTERN = re.compile(r"^State_Graph_v(\d+)\.png$", re.IGNORECASE)


def next_state_graph_version(output_dir: Path) -> int:
    """Next integer for ``State_Graph_v{n}.png`` in ``output_dir`` (starts at 1 if none)."""
    output_dir = output_dir.resolve()
    if not output_dir.is_dir():
        return 1
    best = 0
    for p in output_dir.iterdir():
        if not p.is_file():
            continue
        m = _STATE_GRAPH_PNG_PATTERN.match(p.name)
        if m:
            best = max(best, int(m.group(1)))
    return best + 1


def export_agent_state_graph_png(
    agent: CompiledStateGraph,
    output_dir: Path,
    *,
    xray: bool = True,
) -> Path:
    """Render the compiled LangGraph to PNG via Mermaid (default: Mermaid.Ink API).

    Requires network access unless you switch ``draw_mermaid_png`` to a non-API method.
    """
    out = output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    n = next_state_graph_version(out)
    path = out / f"State_Graph_v{n}.png"
    agent.get_graph(xray=xray).draw_mermaid_png(output_file_path=str(path))
    return path


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


def _print_token_usage(m: Any, stream: Any) -> None:
    """Print token usage info from a message's usage_metadata, if available."""
    usage = getattr(m, "usage_metadata", None)
    if usage:
        parts: list[str] = []
        if isinstance(usage, dict):
            for k in ("input_tokens", "output_tokens", "total_tokens"):
                if usage.get(k) is not None:
                    parts.append(f"{k.replace('_tokens', '')}: {usage[k]}")
        else:
            for k in ("input_tokens", "output_tokens", "total_tokens"):
                v = getattr(usage, k, None)
                if v is not None:
                    parts.append(f"{k.replace('_tokens', '')}: {v}")
        if parts:
            print(f"Tokens: {' | '.join(parts)}", file=stream)


# ---------------------------------------------------------------------------
# Stream agent  (primary execution path)
# ---------------------------------------------------------------------------

_SEP = "━" * 60


# ---------------------------------------------------------------------------
# Tool result size guard
# ---------------------------------------------------------------------------

_MAX_TOOL_RESULT_CHARS = 6_000


def _summarize_tool_result(content: str, tool_name: str) -> str:
    """Replace oversized tool results with a structured summary.

    Preserves key metadata fields (paths, exit codes, status) while discarding
    bulk content (base64, long stdout, binary data) that would overflow the LLM
    context window.  This mirrors how Claude handles tool output compaction —
    the model sees a meaningful summary, not a chopped-off string.
    """
    if len(content) <= _MAX_TOOL_RESULT_CHARS:
        return content

    # tr_* tools always return JSON — parse and keep the useful small fields
    try:
        data = json.loads(content)
        result = data.get("result", {})
        summary: dict[str, Any] = {
            "status": data.get("status", "unknown"),
            "zone": data.get("zone", ""),
            "operation": data.get("operation", ""),
            "tool": tool_name,
            "note": (
                f"[OUTPUT SUPPRESSED — {len(content):,} chars exceeded safe context "
                f"limit of {_MAX_TOOL_RESULT_CHARS:,}. Binary or large content was "
                f"produced but not loaded into context. Reference the file path from "
                f"your TODO list to continue work.]"
            ),
        }
        # Preserve small, actionable fields
        if isinstance(result, dict):
            for key in ("written_to", "deleted", "exit_code", "stderr"):
                if key in result and result[key] is not None:
                    val = result[key]
                    # Keep stderr only if it is short enough to be useful
                    if key == "stderr" and isinstance(val, str) and len(val) > 500:
                        val = val[:500] + " … [truncated]"
                    summary[key] = val
        return json.dumps(summary)
    except Exception:
        # Plain-text tool output — keep a small leading excerpt
        return (
            content[:2_000]
            + f"\n\n[OUTPUT SUPPRESSED — {len(content):,} chars total. "
            "Large or binary output was cut. Write large outputs to a file "
            "in the sandbox and reference the file path instead of printing inline.]"
        )


# ---------------------------------------------------------------------------
# Post-task cleanup (local mode only)
# ---------------------------------------------------------------------------


def _cleanup_local_sandbox(workspace_root: Path, sandbox_root: Path) -> None:
    """Delete sandbox and large_tool_results after a local task completes.

    - sandbox_root: deleted entirely (temp work; output files go to output_dir)
    - workspace_root/large_tool_results: deleted (deepagents eviction cache)
    """
    for target in (sandbox_root, workspace_root / "large_tool_results"):
        if target.exists():
            try:
                shutil.rmtree(target)
                logger.debug("Cleaned up %s", target)
            except Exception as exc:
                logger.warning("Could not remove %s: %s", target, exc)


# ---------------------------------------------------------------------------
# Permission prompt / user question handler
# ---------------------------------------------------------------------------


async def _prompt_permission(interrupt_value: Any) -> str:
    """Render the interrupt payload to the terminal and collect the user's response.

    Dispatches on the validated interrupt type.  Each branch parses the raw dict
    into the corresponding typed model so all field access is safe and explicit.

    Returns the user's response string:
      • "approve" / "reject"  for permission prompts
      • free-text answer      for user questions
    """
    interrupt_type = interrupt_value.get("type", "") if isinstance(interrupt_value, dict) else ""

    # ── tr_ask_user: agent asks a free-text question ───────────────────────
    if interrupt_type == "user_question":
        iv = UserQuestionInterrupt.model_validate(interrupt_value)

        print(f"\n\033[36m{_SEP}\033[0m", file=sys.stderr)
        print("\033[36m💬  AGENT QUESTION\033[0m", file=sys.stderr)
        print(f"\033[36m{_SEP}\033[0m", file=sys.stderr)
        print(f"  {iv.question}", file=sys.stderr)
        if iv.options:
            print(f"  Options: {' | '.join(iv.options)}", file=sys.stderr)
        print(f"\033[36m{_SEP}\033[0m", file=sys.stderr)

        answer: str = await asyncio.to_thread(input, "  Your answer: ")
        response = answer.strip() or "no response"
        print(f"\033[36m  → {response}\033[0m\n", file=sys.stderr)
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
        print(f"\033[35m{_SEP}\033[0m", file=sys.stderr)

        answer = await asyncio.to_thread(input, "  Allow? [y/N]: ")
        decision = "approve" if answer.strip().lower() in ("y", "yes") else "reject"
        if decision == "approve":
            print("\033[32m  ✅  Approved\033[0m\n", file=sys.stderr)
        else:
            print("\033[31m  🚫  Rejected\033[0m\n", file=sys.stderr)
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
    print(f"\033[33m{_SEP}\033[0m", file=sys.stderr)

    answer = await asyncio.to_thread(input, "  Allow? [y/N]: ")
    decision = "approve" if answer.strip().lower() in ("y", "yes") else "reject"
    if decision == "approve":
        print("\033[32m  ✅  Approved — running command\033[0m\n", file=sys.stderr)
    else:
        print("\033[31m  🚫  Rejected — command will not run\033[0m\n", file=sys.stderr)
    return decision


async def astream_agent(
    agent: CompiledStateGraph,
    task: str,
    *,
    thread_id: str | None = None,
    recursion_limit: int = 200,
) -> str:
    """
    Run the agent with real-time async streaming.

    - LLM tokens are printed to stderr as they arrive (no buffering).
    - Tool calls and results are logged at INFO level with clear labels.
    - Handles ``interrupt()`` events from ``PermissionMiddleware``: pauses,
      asks the user on stdin, then resumes the graph with approve/reject.
    - Returns the final assistant text.
    """
    cfg: RunnableConfig = {
        "recursion_limit": recursion_limit,
        "configurable": {"thread_id": thread_id or str(uuid.uuid4())},
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
        interrupted_value: Any = None

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
                if interrupts:
                    iv = interrupts[0]
                    interrupted_value = iv.value if hasattr(iv, "value") else iv
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
                        final_messages.append(m)

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
                            # Guard against large results (base64, long stdout, binary)
                            # overflowing the LLM context on the next call.
                            safe_body = _summarize_tool_result(body, tn)
                            if safe_body is not body:
                                m.content = safe_body
                            preview = safe_body if len(safe_body) <= 600 else safe_body[:600] + "\n    … [truncated]"
                            logger.info("")
                            logger.info("\033[32m✅  TOOL RESULT ← %s\033[0m", tn)
                            logger.info("    %s", preview.replace("\n", "\n    "))

        # End of this stream pass — close any dangling AI output line
        if _in_ai_stream:
            print("\n", file=sys.stderr)

        # No interrupt → done
        if interrupted_value is None:
            break

        # Ask the user, then resume the graph
        decision = await _prompt_permission(interrupted_value)
        current_input = Command(resume=decision)

    logger.info("")
    logger.info(_SEP)
    logger.info("YUYUTSAVA  task complete")
    logger.info(_SEP)

    return last_assistant_text(final_messages)


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())


