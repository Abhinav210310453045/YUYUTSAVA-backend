"""What each configured MCP server is doing, and how the agent is told.

The agent had no idea which MCP connectors it had. A server that failed to
start was a line in the daemon log; asked about its connectors, the agent went
looking for Claude Desktop's MCP configuration instead. The manager now records
a :class:`ServerStatus` per configured server, and :func:`connectors_block`
turns the servers in an agent's scope into a few prompt lines: which are
connected (and how to load their tools), which failed and why, and where the
user's configuration lives. The same records serve ``GET /mcp/status`` and the
daemon's ready summary.

Free of the MCP SDK.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from yuyutsava.mcp.naming import SEPARATOR

CONNECTED = "connected"
FAILED = "failed"
#: On its way up: its tools join as soon as it is.
STARTING = "starting"
#: Configured, but not running and not known to have failed — stopped by a reload.
NOT_RUNNING = "not running"


@dataclass(frozen=True)
class ServerStatus:
    """One configured MCP server."""

    name: str
    state: str
    tools: int = 0
    error: str = ""
    #: The file that configured it.
    source: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        """``probe (41 tools)`` or ``notion — failed: …`` — one line for logs."""
        if self.state == CONNECTED:
            return f"{self.name} ({self.tools} tool{'s' if self.tools != 1 else ''})"
        return f"{self.name} — {self.state}{f': {self.error}' if self.error else ''}"


def _tilde(path: Path | str) -> str:
    p = Path(path)
    try:
        return f"~/{p.relative_to(Path.home()).as_posix()}"
    except ValueError:
        return str(p)


def connectors_block(statuses: list[ServerStatus], config_path: Path | str) -> str:
    """Prompt lines naming the agent's MCP connectors; ``""`` when it has none.

    Empty rather than "no connectors" when nothing is configured, so an agent
    without MCP gets exactly the prompt it always had.
    """
    if not statuses:
        return ""
    lines = [
        "",
        f"MCP connectors — the user's MCP servers, configured in {_tilde(config_path)}:",
    ]
    for s in statuses:
        if s.state == CONNECTED:
            lines.append(f"  - {s.name}: connected, {s.tools} tools ({s.name}{SEPARATOR}*)")
        elif s.state == STARTING:
            lines.append(f"  - {s.name}: still starting — its tools are on their way")
        else:
            reason = f" — {s.error}" if s.error else ""
            lines.append(f"  - {s.name}: {s.state}{reason}")
    lines += [
        "Load a connector's tools like any other: tool_search('<server>__*') or a keyword.",
        "If one is still starting, it has not failed: tell the user it is starting and its",
        "tools will work from their next message.",
        "If one failed, tell the user it failed and why; they fix it in that file.",
        "These are the only connectors you have. Never read another app's MCP configuration",
        "(Claude Desktop, Cursor, …) to answer a question about yours.",
    ]
    return "\n".join(lines)
