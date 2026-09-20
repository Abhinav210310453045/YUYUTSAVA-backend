"""Command-line tooling that is not about building an agent.

- :func:`setup_logging` — the terminal log handler for the one-shot command
- :func:`builtin_tools_reference_json` — the ``--print-tools`` reference
- :func:`export_agent_state_graph_png` — ``--generate_agent_graph``'s Mermaid PNG
- :func:`cleanup_local_sandbox` — removes a finished task's per-workspace scratch
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from yuyutsava.ansi import BOLD_RED, DIM, RED, RESET, YELLOW
from yuyutsava.config.env import env_flag
from yuyutsava.config.logs import silence_plumbing_loggers
from yuyutsava.storage.paths import WorkspaceLayout

logger = logging.getLogger("yuyutsava.cli.tooling")


def setup_logging(verbose: bool = False, *, debug_plumbing: bool = False) -> None:
    """Configure the ``yuyutsava`` logger for the CLI to print to stderr.

    Root logger is kept at WARNING; only the ``yuyutsava`` namespace is
    flipped to DEBUG (verbose) or INFO (default). Plumbing loggers
    (uvicorn / langgraph_api / httpx / …) are silenced regardless of
    verbose so the chat REPL stays uncluttered. Pass
    ``debug_plumbing=True`` (or set ``YUYUTSAVA_DEBUG_PLUMBING=1``) to
    see those — useful only when debugging the runtime itself.
    """
    if not debug_plumbing:
        debug_plumbing = env_flag("YUYUTSAVA_DEBUG_PLUMBING", true=("1", "true", "yes"))

    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)

    class _Fmt(logging.Formatter):
        _COLOURS = {
            logging.DEBUG: f"{DIM}",  # dim
            logging.INFO: f"{RESET}",  # normal
            logging.WARNING: f"{YELLOW}",  # yellow
            logging.ERROR: f"{RED}",  # red
            logging.CRITICAL: f"{BOLD_RED}",  # bold red
        }
        _RESET = f"{RESET}"

        def format(self, record: logging.LogRecord) -> str:
            colour = self._COLOURS.get(record.levelno, "")
            msg = super().format(record)
            return f"{colour}{msg}{self._RESET}"

    # Verbose: prefix with timestamp + logger name so the user can tell which
    # subsystem is talking. Plain ``%(message)s`` otherwise to keep
    # non-verbose output uncluttered.
    fmt = "%(asctime)s %(name)s | %(message)s" if verbose else "%(message)s"
    handler.setFormatter(_Fmt(fmt, datefmt="%H:%M:%S"))

    # Root stays at WARNING so unrelated libraries don't flood stderr.
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level < logging.WARNING:
        root.setLevel(logging.WARNING)

    log = logging.getLogger("yuyutsava")
    log.setLevel(level)
    log.handlers.clear()
    log.addHandler(handler)
    log.propagate = False

    if not debug_plumbing:
        silence_plumbing_loggers()


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


def cleanup_local_sandbox(layout: WorkspaceLayout) -> None:
    """Delete the sandbox + deepagents scratch after a CLI task completes.

    - ``layout.sandbox``: deleted entirely (temp work; deliverables live in
      ``outputs/``, reusable scripts in ``scripts/`` — both untouched)
    - ``layout.tmp/*``: deepagents' eviction cache (``large_tool_results``) and
      summarization dumps (``conversation_history``); the durable copies are in
      the artifacts / transcript_messages DB tables.

    Runs in local AND docker mode — the sandbox is a host dir under
    ``.yuyutsava`` either way. Callers invoke it after ``AgentBundle.aclose()``
    so no container still has the directory as its cwd. The daemon cleans the
    same scratch via the UnifiedSweeper's TTL targets instead, since its
    workspace is long-lived and shared across tasks.
    """
    targets: list[Path] = [layout.sandbox]
    if layout.tmp.is_dir():
        targets.extend(layout.tmp.iterdir())
    for target in targets:
        if not target.exists():
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            logger.debug("Cleaned up %s", target)
        except Exception as exc:
            logger.warning("Could not remove %s: %s", target, exc)
