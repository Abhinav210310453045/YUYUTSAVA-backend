"""
Build a **Deep Agents** graph with an OpenAI-compatible chat model and a real-disk backend.

Uses ``LocalShellBackend`` (filesystem + ``execute``) so built-in ``read_file`` /
``write_file`` / ``ls`` / … map to the workspace root, and ``execute`` runs shell
on the host per deepagents (see ``deepagents.backends.LocalShellBackend``).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph

from yuyutsava.core.config import DockerSettings, LlmSettings
from yuyutsava.core.docker_sandbox_backend import DockerSandboxBackend
from yuyutsava.core.llm import chat_model

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


def _local_system_prompt(workspace_root: Path) -> str:
    root = workspace_root.resolve()
    return f"""\
YUYUTSAVA workspace (real disk + local shell):
Root: {root}

- File tools (``read_file``, ``write_file``, ``edit_file``, ``ls``, ``glob``, ``grep``) use this root. \
With ``virtual_mode=True``, paths are virtual and anchored here (e.g. ``/yuyutsava/workspace/README.txt``).
- For shell, use the built-in **execute** tool. Commands run on the real host machine with cwd at \
the root above; they are NOT sandboxed.
- **IMPORTANT: Treat {root} as your sandbox boundary. Do NOT read, write, or execute anything \
outside this directory. Never use system paths like /tmp, /var, /home, /usr, /etc, or any path \
outside the workspace, unless the user explicitly instructs you to do so.**

## Data Processing Strategy (confined to workspace)
For tasks involving structured files (.xlsx, .csv, .json, binary files) or any computation:
1. Write a self-contained Python script into the workspace: ``{root}/_task.py``
2. Execute it with the **execute** tool: ``python3 {root}/_task.py``
3. Capture the printed output as your result
4. Delete the script after use: ``rm {root}/_task.py``
Do NOT run multi-step logic as inline shell one-liners — always write a script to the workspace first.

Complete the user's task; be concise."""


def _docker_system_prompt(workspace_root: Path, export_host: Path | None) -> str:
    root = workspace_root.resolve()
    extra = ""
    if export_host is not None:
        exp = export_host.resolve()
        extra = (
            f"\n- A second host directory is bind-mounted at **/output** in the container (host: {exp}). "
            "Write deliverables you want isolated there under paths like ``/output/report.txt``."
        )
    return f"""\
YUYUTSAVA workspace runs inside a **Docker sandbox** container (fully isolated from your host shell).

- The host directory **{root}** is mounted at **/workspace** in the container. \
Virtual paths like ``/yuyutsava/foo.txt`` refer to files under that mount.{extra}
- Use the built-in **execute** tool for shell commands; they run **inside the sandbox container**, \
never on the host machine.
- **IMPORTANT: All work must stay inside /workspace. Do NOT write to or execute from container \
system directories like /tmp, /etc, /home, /usr, /root, or any path outside /workspace, \
unless the user explicitly instructs you to do so.**

## Data Processing Strategy (inside the sandbox)
For tasks involving structured files (.xlsx, .csv, .json, binary files) or any computation:
1. Write a self-contained Python script into the sandbox workspace: ``/workspace/_task.py``
2. Execute it inside the sandbox with the **execute** tool: ``python3 /workspace/_task.py``
3. Capture the printed output as your result
4. Delete the script after use: ``rm /workspace/_task.py``
Do NOT run multi-step logic as inline shell one-liners — always write a script to /workspace first.

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
) -> AgentBundle:
    """Build a Deep Agent; ``local`` uses ``LocalShellBackend``, ``docker`` uses ``DockerSandboxBackend``."""
    model = chat_model(settings)
    if execution_mode == "docker":
        docker_cfg = docker_settings or DockerSettings()
        export = docker_cfg.export_dir.resolve() if docker_cfg.export_dir else None
        docker_backend = DockerSandboxBackend(
            image=docker_cfg.image,
            workspace_host=workspace_root.resolve(),
            export_host=export,
            network=docker_cfg.network,
            timeout=bash_timeout_sec,
            memory=docker_cfg.memory,
            cpus=docker_cfg.cpus,
            pids_limit=docker_cfg.pids_limit,
        )
        graph = create_deep_agent(
            model=model,
            backend=docker_backend,
            system_prompt=_docker_system_prompt(workspace_root, docker_cfg.export_dir),
            debug=False,
        )
        return AgentBundle(agent=graph, docker_backend=docker_backend)

    backend = _local_shell_backend_factory(workspace_root, bash_timeout_sec)
    graph = create_deep_agent(
        model=model,
        backend=backend,
        system_prompt=_local_system_prompt(workspace_root),
        debug=False,
    )
    return AgentBundle(agent=graph, docker_backend=None)


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


def stream_agent(
    agent: CompiledStateGraph,
    task: str,
    *,
    recursion_limit: int = 200,
) -> str:
    """
    Run the agent with real-time streaming.

    - LLM tokens are printed to stderr as they arrive (no buffering).
    - Tool calls and results are logged at INFO level with clear labels.
    - Returns the final assistant text (same contract as ``invoke_agent``).
    """
    cfg: dict[str, Any] = {"recursion_limit": recursion_limit}

    logger.info(_SEP)
    logger.info("YUYUTSAVA  starting task")
    logger.info(_SEP)
    logger.info("Task: %s", task)
    logger.info(_SEP)

    # We stream with two modes at once:
    #   "messages" → yields (mode, (chunk, metadata)) — LLM tokens as they arrive
    #   "updates"  → yields (mode, {"node": state_delta}) — tool calls / results
    stream = agent.stream(
        {"messages": [HumanMessage(content=task)]},
        config=cfg,
        stream_mode=["messages", "updates"],
    )

    final_messages: list[Any] = []
    _in_ai_stream = False          # are we mid-stream of an AI response?
    _current_tool_name: str = ""   # last tool being called

    for event in stream:
        # With multiple stream_mode values, events are (mode, data) tuples
        if not isinstance(event, tuple) or len(event) != 2:
            continue
        mode, data = event

        # ── messages mode: streaming LLM tokens ────────────────────────────
        if mode == "messages":
            chunk, _meta = data
            # AI text token
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
                        # Print header before first token of this response
                        print(f"\n\033[36m{'─' * 60}\033[0m", file=sys.stderr)
                        print("\033[36m🤖  AI (streaming)\033[0m", file=sys.stderr)
                        print(f"\033[36m{'─' * 60}\033[0m", file=sys.stderr)
                        _in_ai_stream = True
                    # Print token immediately, no newline
                    print(text, end="", flush=True, file=sys.stderr)

                # Tool call chunks — show which tool is being invoked
                if chunk.tool_calls:
                    if _in_ai_stream:
                        print("\n", file=sys.stderr)  # end the streaming line
                        _in_ai_stream = False
                    for tc in chunk.tool_calls:
                        name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                        if name and name != _current_tool_name:
                            _current_tool_name = name
                            logger.info("")
                            logger.info("\033[33m🔧  TOOL CALL → %s\033[0m", name)

                # Partial tool_call_chunks (streaming the args)
                if getattr(chunk, "tool_call_chunks", None):
                    for tcc in chunk.tool_call_chunks:
                        name = tcc.get("name", "") if isinstance(tcc, dict) else getattr(tcc, "name", "")
                        if name and name != _current_tool_name:
                            _current_tool_name = name
                            logger.info("")
                            logger.info("\033[33m🔧  TOOL CALL → %s\033[0m", name)

            # Tool result coming back
            elif isinstance(chunk, ToolMessage):
                if _in_ai_stream:
                    print("\n", file=sys.stderr)
                    _in_ai_stream = False
                tn = getattr(chunk, "name", "tool") or "tool"
                body = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                preview = body if len(body) <= 400 else body[:400] + " … [truncated]"
                logger.info("")
                logger.info("\033[32m✅  TOOL RESULT ← %s\033[0m", tn)
                logger.info("    %s", preview.replace("\n", "\n    "))

        # ── updates mode: full state delta after each node ─────────────────
        elif mode == "updates":
            if _in_ai_stream:
                print("\n", file=sys.stderr)
                _in_ai_stream = False

            if not isinstance(data, dict):
                continue

            for node_name, node_data in data.items():
                if not isinstance(node_data, dict):
                    continue
                msgs = node_data.get("messages", [])
                if not isinstance(msgs, list):
                    continue

                for m in msgs:
                    final_messages.append(m)

                    if isinstance(m, AIMessage):
                        # Log token usage if present
                        usage = getattr(m, "usage_metadata", None)
                        if usage:
                            parts_u: list[str] = []
                            for k in ("input_tokens", "output_tokens", "total_tokens"):
                                v = usage.get(k) if isinstance(usage, dict) else getattr(usage, k, None)
                                if v is not None:
                                    parts_u.append(f"{k.replace('_tokens', '')}: {v}")
                            if parts_u:
                                logger.debug("    Tokens  %s", " | ".join(parts_u))

                        # Full tool call args (from the completed message)
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
                        preview = body if len(body) <= 600 else body[:600] + "\n    … [truncated]"
                        logger.info("")
                        logger.info("\033[32m✅  TOOL RESULT ← %s\033[0m", tn)
                        logger.info("    %s", preview.replace("\n", "\n    "))

    # Make sure we end the streaming line cleanly
    if _in_ai_stream:
        print("\n", file=sys.stderr)

    logger.info("")
    logger.info(_SEP)
    logger.info("YUYUTSAVA  task complete")
    logger.info(_SEP)

    return last_assistant_text(final_messages)


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())


# ---------------------------------------------------------------------------
# Legacy invoke_agent (kept for compatibility; wraps stream_agent)
# ---------------------------------------------------------------------------


def invoke_agent(
    agent: CompiledStateGraph,
    task: str,
    *,
    verbose: bool = False,  # noqa: ARG001 — kept for API compatibility; streaming is always on
    recursion_limit: int = 200,
) -> str:
    """
    Backward-compatible wrapper.  ``verbose`` is now ignored — streaming is
    always active via ``stream_agent``.
    """
    return stream_agent(agent, task, recursion_limit=recursion_limit)
