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

from yuyutsava.core.config import DockerSettings, LocalSettings, LlmSettings, SearchConfig
from yuyutsava.core.docker_sandbox_backend import DockerSandboxBackend
from yuyutsava.core.llm import chat_model
from yuyutsava.core.permission_middleware import PermissionMiddleware
from yuyutsava.core.tracing import get_callback
from yuyutsava.core.tool_filter_middleware import ToolFilterMiddleware
from yuyutsava.core.tool_registry import ToolRegistry
from yuyutsava.agents.task_runner.tools import bind_tools as _bind_task_runner_tools
from yuyutsava.agents.task_runner.prompts import task_runner_rules_section
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.skills.tools import make_skill_tools
from yuyutsava.tools.search import make_search_tools
from yuyutsava.models.tool_messages import SuppressedContentNotice, SuppressedReason
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


_TOOL_DISCOVERY_SECTION = """\
## TOOL DISCOVERY

You have access to tool_search to discover available tools on demand.
ALWAYS call tool_search before using any tool whose schema you don't know yet.

Common patterns:
  tool_search('tr_*')        → TaskRunner tools (read, write, delete, execute, grep, ask_user)
  tool_search('ws_*')        → Web search tools (Tavily, Exa — only if API keys configured)
  tool_search('tr_execute')  → Permission-gated shell with full network/internet access
  tool_search('sk_*')        → Skills tools (read/write reusable patterns)

Quick tools you can call without loading schemas first:
  tr_ask_user(question, options) — ask the user a question and get their response.
    Use this whenever you need clarification, a decision, or confirmation before acting.
    Call it directly; no need to run tool_search first.

Rules:
- Start every task by calling tool_search('tr_*') to see what is available.
- For internet access (curl, API calls, web scraping): use tr_execute (requires user approval)
  OR ws_tavily_search / ws_exa_search (no approval needed, structured results).
- Do NOT guess tool parameter schemas — always check via tool_search first.
- tr_execute_in_sandbox runs WITHOUT network access (isolated sandbox).
  Use tr_execute for commands that need the internet.

## SKILL REFLECTION (after every completed task)

Before finishing, ask yourself: did this task follow a reusable pattern?
A pattern is worth saving if it:
- Combined multiple tools in a non-obvious sequence.
- Required a specific workaround or approach the next agent wouldn't guess.
- Could apply to similar future tasks (not one-off or workspace-specific).

If yes: call sk_write_skill(name, description, body) to save it to personal scope.
Keep the body concise (≤ 150 words): what was done, which tools were used, any gotchas.
If no clear reusable pattern: skip it — do not save trivial or one-off tasks as skills.
"""


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

{_TOOL_DISCOVERY_SECTION}
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

{_TOOL_DISCOVERY_SECTION}
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


def _build_tool_registry_and_tools(
    task_runner_tools: list,
    search_config: SearchConfig | None,
    skill_registry: SkillRegistry | None,
) -> tuple[list, Any]:
    """Build a ToolRegistry and return (startup_tools, registry).

    startup_tools = [tool_search] + all_custom_tools
    All custom tools go into the graph for execution; only tool_search is
    visible to the LLM upfront (the ToolFilterMiddleware hides tr_*/ws_* etc.).
    """
    search_tools = make_search_tools(search_config) if search_config else []
    skill_tools = make_skill_tools(skill_registry) if skill_registry else []
    all_custom_tools = task_runner_tools + search_tools + skill_tools

    registry = ToolRegistry()
    registry.register_many(all_custom_tools)
    tool_search = registry.make_tool_search_tool()

    # tool_search first so it appears first in the graph's tool list.
    # ToolFilterMiddleware hides all tr_* / ws_* / sk_* from the LLM; only
    # tool_search is visible, driving the lazy-discovery pattern.
    startup_tools = [tool_search] + all_custom_tools
    return startup_tools, registry


def build_agent(
    workspace_root: Path,
    settings: LlmSettings,
    *,
    bash_timeout_sec: int = 120,
    execution_mode: Literal["local", "docker"] = "local",
    docker_settings: DockerSettings | None = None,
    local_settings: LocalSettings | None = None,
    permission_check: bool = True,
    search_config: SearchConfig | None = None,
) -> AgentBundle:
    """Build a Deep Agent; ``local`` uses ``LocalShellBackend``, ``docker`` uses ``DockerSandboxBackend``.

    Args:
        permission_check: When ``True`` (default), attaches ``PermissionMiddleware`` so
            the agent pauses and asks the user before running dangerous shell commands.
            Pass ``False`` to disable (e.g. in automated / non-interactive pipelines).
        search_config: When provided, ws_* web search tools are added and made
            discoverable via tool_search('ws_*'). Pass ``SearchConfig.from_env()``
            to auto-load from TAVILY_API_KEY / EXA_API_KEY env vars.
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

    skill_registry = SkillRegistry(workspace_dir=ws)

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
        startup_tools, _ = _build_tool_registry_and_tools(
            _bind_task_runner_tools(ws), search_config, skill_registry
        )
        graph = create_deep_agent(
            model=model,
            tools=startup_tools,
            backend=docker_backend,
            system_prompt=_docker_system_prompt(workspace_root, docker_cfg.export_dir),
            checkpointer=checkpointer,
            middleware=middleware,
            debug=False,
        )
        return AgentBundle(agent=graph, docker_backend=docker_backend)

    backend = _local_shell_backend_factory(workspace_root, bash_timeout_sec)
    startup_tools, _ = _build_tool_registry_and_tools(
        _bind_task_runner_tools(ws, sandbox_root), search_config, skill_registry
    )
    graph = create_deep_agent(
        model=model,
        tools=startup_tools,
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

# Absolute ceiling: results larger than this are never passed to the LLM as-is.
# tr_read_file results already carry pagination metadata + a SuppressedContentNotice
# when truncated, so legitimate file reads never hit this unless the single requested
# chunk is itself enormous.  This backstop catches only true runaway cases:
# binary blobs accidentally read as text, or pathological stdout.
_MAX_TOOL_RESULT_CHARS = 100_000

# Per-tool softer limits used when constructing SuppressedContentNotice payloads.
_MAX_STDOUT_CHARS = 40_000


def _guard_tool_result(content: str, tool_name: str) -> str:
    """Enforce the hard ceiling on tool results entering the LLM context.

    tr_read_file results are handled at the executor/agent layer — they already
    embed a SuppressedContentNotice in result.truncation_notice when has_more is
    True.  This function is the last-resort backstop for anything that still
    exceeds _MAX_TOOL_RESULT_CHARS after that processing.

    For tr_execute_in_sandbox stdout overflow a SuppressedContentNotice is
    injected into the result JSON so the LLM gets actionable recovery hints
    rather than a blind truncation.

    For any other tool a minimal notice replaces the bulk payload.
    """
    if len(content) <= _MAX_TOOL_RESULT_CHARS:
        return content

    try:
        data = json.loads(content)
        result = data.get("result") or {}

        # ── tr_execute_in_sandbox: inject notice into result, keep exit_code ──
        if tool_name in ("tr_execute_in_sandbox", "tr_grep"):
            command = ""
            if isinstance(result, dict):
                command = str(result.get("stdout", ""))[:80]  # best-effort for hint

            notice = SuppressedContentNotice.stdout_too_large(
                tool=tool_name,
                command=command,
                original_size_chars=len(content),
            )
            # Preserve exit_code and stderr (small); replace stdout with notice
            new_result: dict[str, Any] = {
                "kind": result.get("kind", "shell") if isinstance(result, dict) else "shell",
                "exit_code": result.get("exit_code") if isinstance(result, dict) else None,
                "stderr": (result.get("stderr", "") or "")[:2_000] if isinstance(result, dict) else "",
                "stdout_notice": notice.model_dump(),
            }
            data["result"] = new_result
            return json.dumps(data)

        # ── all other tools: replace entire result with a generic notice ──────
        notice = SuppressedContentNotice(
            reason=SuppressedReason.UNKNOWN,
            original_size_chars=len(content),
            tool=tool_name,
            human_message=(
                f"Tool result was {len(content):,} chars — too large to pass to the LLM. "
                f"Write large outputs to a file in the sandbox and reference the path."
            ),
            recovery=[],
        )
        data["result"] = notice.model_dump()
        return json.dumps(data)

    except Exception:
        # Non-JSON tool output (shouldn't happen for tr_* tools but be safe)
        notice = SuppressedContentNotice(
            reason=SuppressedReason.UNKNOWN,
            original_size_chars=len(content),
            tool=tool_name,
            human_message=(
                f"Tool output was {len(content):,} chars and could not be parsed. "
                f"Write large outputs to a file and reference the path."
            ),
            recovery=[],
        )
        return json.dumps({"suppressed": True, "notice": notice.model_dump()})


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


# ---------------------------------------------------------------------------
# Typed stream events  (consumed by the daemon's channel router)
# ---------------------------------------------------------------------------


from dataclasses import dataclass as _dataclass


@_dataclass(frozen=True)
class StreamEvent:
    """Structured event yielded by ``astream_agent_iter``.

    ``kind``:
      - ``token``       data={"text": str}
      - ``tool_call``   data={"name": str, "args": dict}
      - ``tool_result`` data={"name": str, "preview": str}
      - ``log``         data={"text": str}
      - ``final``       data={"text": str}   (last assistant message)
    """

    kind: str
    data: dict


async def astream_agent_iter(
    agent: CompiledStateGraph,
    task: str,
    *,
    thread_id: str | None = None,
    recursion_limit: int = 200,
    ask_handler=None,  # async (interrupt_value: dict) -> str
    run_name: str = "agent",
):
    """Async generator that yields ``StreamEvent``s instead of printing them.

    The daemon uses this to feed events into the channel router. ``ask_handler``
    is called whenever the graph emits an ``interrupt`` — the daemon plugs in
    a function that routes through the web/terminal channel and returns the
    user's decision string. If ``ask_handler`` is None, interrupts are
    auto-rejected (suitable for headless / autonomous runs).

    Yields events; the final yielded event is always ``StreamEvent("final", {"text": ...})``.
    """
    _tid = thread_id or str(uuid.uuid4())
    cfg: RunnableConfig = {
        "recursion_limit": recursion_limit,
        "configurable": {"thread_id": _tid},
    }
    _lf_cb = get_callback(session_id=_tid, run_name=run_name)
    if _lf_cb is not None:
        cfg["callbacks"] = [_lf_cb]

    final_messages: list[Any] = []
    current_input: Any = {"messages": [HumanMessage(content=task)]}

    while True:
        interrupted_value: Any = None
        _steps_this_pass = 0

        async for event in agent.astream(
            current_input, config=cfg, stream_mode=["messages", "updates"],
        ):
            if not isinstance(event, tuple) or len(event) != 2:
                continue
            mode, data = event

            if mode == "updates" and isinstance(data, dict) and "__interrupt__" in data:
                interrupts = data["__interrupt__"]
                if interrupts:
                    iv = interrupts[0]
                    interrupted_value = iv.value if hasattr(iv, "value") else iv
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
                            safe_body = _guard_tool_result(body, tn)
                            if safe_body is not body:
                                m.content = safe_body
                            preview = safe_body if len(safe_body) <= 600 else safe_body[:600] + " …[truncated]"
                            yield StreamEvent("tool_result", {"name": tn, "preview": preview})

        if interrupted_value is None:
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

        if ask_handler is None:
            decision = "reject"
        else:
            try:
                decision = await ask_handler(interrupted_value)
            except Exception as exc:
                yield StreamEvent("log", {"text": f"ask_handler raised: {exc}; rejecting"})
                decision = "reject"
        current_input = Command(resume=decision)


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
    _tid = thread_id or str(uuid.uuid4())
    cfg: RunnableConfig = {
        "recursion_limit": recursion_limit,
        "configurable": {"thread_id": _tid},
    }
    _lf_cb = get_callback(session_id=_tid, run_name="cli")
    if _lf_cb is not None:
        cfg["callbacks"] = [_lf_cb]

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
                            # Last-resort guard: replaces runaway payloads with a
                            # SuppressedContentNotice before they reach the LLM.
                            # tr_read_file results are already handled at the executor
                            # layer; this only fires for pathological cases.
                            safe_body = _guard_tool_result(body, tn)
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
            if _steps_this_pass == 0 and not final_messages:
                logger.error(
                    "⚠️  Agent produced no output — possible recursion limit hit "
                    "(limit=%d) or graph exited unexpectedly. "
                    "Try re-running with --recursion-limit <higher value>.",
                    recursion_limit,
                )
            break

        # Ask the user, then resume the graph
        decision = await _prompt_permission(interrupted_value)
        current_input = Command(resume=decision)

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


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())


