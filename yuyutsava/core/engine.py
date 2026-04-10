"""
Build a **Deep Agents** graph with an OpenAI-compatible chat model and a real-disk backend.

Uses ``LocalShellBackend`` (filesystem + ``execute``) so built-in ``read_file`` /
``write_file`` / ``ls`` / … map to the workspace root, and ``execute`` runs shell
on the host per deepagents (see ``deepagents.backends.LocalShellBackend``).
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph

from yuyutsava.core.config import LlmSettings
from yuyutsava.core.docker_sandbox_backend import DockerSandboxBackend
from yuyutsava.core.llm import chat_model


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


def _local_system_prompt(workspace_root: Path) -> str:
    root = workspace_root.resolve()
    return f"""\
YUYUTSAVA workspace (real disk + local shell):
Root: {root}

- File tools (``read_file``, ``write_file``, ``edit_file``, ``ls``, ``glob``, ``grep``) use this root. \
With ``virtual_mode=True``, paths are virtual and anchored here (e.g. ``/yuyutsava/workspace/README.txt``).
- For shell, use the built-in **execute** tool. Commands run on your host with cwd at the root above; \
they are not sandboxed — use only in trusted environments.

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
YUYUTSAVA workspace runs inside a **Docker** container (isolated from your host shell).

- The host directory **{root}** is mounted at **/workspace** in the container. \
Virtual paths like ``/yuyutsava/foo.txt`` refer to files under that mount.{extra}
- Use the built-in **execute** tool for shell commands; they run **inside the container**, not on your host.
- Python 3 is available for file-tool internals; prefer **execute** for ad-hoc shell.

Complete the user's task; be concise."""


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


def build_agent(
    workspace_root: Path,
    settings: LlmSettings,
    *,
    bash_timeout_sec: int = 120,
    execution_mode: Literal["local", "docker"] = "local",
    docker_image: str = "deepagent-sandbox:local",
    docker_export_dir: Path | None = None,
    docker_network: Literal["bridge", "none"] = "bridge",
) -> AgentBundle:
    """Build a Deep Agent; ``local`` uses ``LocalShellBackend``, ``docker`` uses ``DockerSandboxBackend``."""
    model = chat_model(settings)
    if execution_mode == "docker":
        export = docker_export_dir.resolve() if docker_export_dir else None
        docker_backend = DockerSandboxBackend(
            image=docker_image,
            workspace_host=workspace_root.resolve(),
            export_host=export,
            network=docker_network,
            timeout=bash_timeout_sec,
        )
        graph = create_deep_agent(
            model=model,
            backend=docker_backend,
            system_prompt=_docker_system_prompt(workspace_root, docker_export_dir),
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


def invoke_agent(
    agent: CompiledStateGraph,
    task: str,
    *,
    verbose: bool = False,
    recursion_limit: int = 200,
) -> str:
    cfg: dict[str, Any] = {"recursion_limit": recursion_limit}
    result = agent.invoke({"messages": [HumanMessage(content=task)]}, config=cfg)
    messages = result.get("messages") or []

    if verbose:
        sep = "━" * 50
        for m in messages:
            if isinstance(m, HumanMessage):
                print(f"\n{sep}", file=sys.stderr)
                print("👤 Human", file=sys.stderr)
                print(f"{sep}", file=sys.stderr)
                content = m.content if isinstance(m.content, str) else str(m.content)
                print(f"Message: {content}", file=sys.stderr)
                _print_token_usage(m, sys.stderr)

            elif isinstance(m, AIMessage):
                print(f"\n{sep}", file=sys.stderr)
                print("🤖 AI", file=sys.stderr)
                print(f"{sep}", file=sys.stderr)
                t = _ai_message_text(m)
                if t:
                    print(f"Message: {t}", file=sys.stderr)
                _print_token_usage(m, sys.stderr)
                if m.tool_calls:
                    for tc in m.tool_calls:
                        name = tc.get("name", "unknown") if isinstance(tc, dict) else getattr(tc, "name", "unknown")
                        args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                        args_str = json.dumps(args, indent=2) if isinstance(args, dict) else str(args)
                        print(f"\n  🔧 Tool Call: {name}", file=sys.stderr)
                        print(f"     Input: {args_str}", file=sys.stderr)

            elif isinstance(m, ToolMessage):
                tn = getattr(m, "name", "tool")
                body = m.content if isinstance(m.content, str) else str(m.content)
                preview = body if len(body) < 500 else body[:500] + "\n     ... [truncated]"
                print(f"\n  🔨 Tool Result: {tn}", file=sys.stderr)
                print(f"     Output: {preview}", file=sys.stderr)

    return last_assistant_text(messages)
