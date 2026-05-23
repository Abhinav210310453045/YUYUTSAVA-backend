"""MCP config dataclasses + loader from ``~/.yuyutsava/mcp_config.json``.

Schema mirrors Claude Code's so users can copy-paste configs::

    {
      "mcpServers": {
        "deepface":    {"command": "python", "args": ["-m", "..."], "env": {}},
        "filesystem":  {"command": "npx", "args": ["-y", "@.../server-filesystem", "~/Documents"]},
        "spotify":     {"url": "http://localhost:8765/mcp"}
      },
      "scopes": {
        "orchestrator":   ["spotify"],
        "file-organizer": ["filesystem"]
      },
      "default_scope": []
    }

- ``mcpServers``: name → either stdio (``command``, ``args``, ``env``) or
  SSE (``url``).
- ``scopes``: agent-name → list of server names whose tools that agent
  receives. Agents missing from ``scopes`` receive ``default_scope``.
- The orchestrator name is a special key (``"orchestrator"``); subagent
  names are their ``BaseSubAgent.name``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yuyutsava.storage.paths import state_dir

logger = logging.getLogger("yuyutsava.mcp.config")


@dataclass(frozen=True)
class MCPServerSpec:
    """One MCP server entry. Exactly one of (command,args) or (url) is set."""

    name: str
    # stdio transport
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    # sse transport
    url: str = ""
    # safety cap: orchestrator prompt can't absorb 1000s of tools
    max_tools: int = 32

    @property
    def transport(self) -> str:
        return "sse" if self.url else "stdio"

    def validate(self) -> None:
        if self.url and self.command:
            raise ValueError(f"server {self.name!r}: set either 'url' or 'command', not both")
        if not self.url and not self.command:
            raise ValueError(f"server {self.name!r}: must have 'command' or 'url'")


@dataclass(frozen=True)
class MCPConfig:
    """Loaded ``mcp_config.json``."""

    servers: dict[str, MCPServerSpec]
    scopes: dict[str, list[str]]
    default_scope: list[str]

    @classmethod
    def empty(cls) -> MCPConfig:
        return cls(servers={}, scopes={}, default_scope=[])

    @classmethod
    def from_file(cls, path: Path | None = None) -> MCPConfig:
        """Load ``mcp_config.json``; return :meth:`empty` if the file is absent."""
        if path is None:
            path = state_dir() / "mcp_config.json"
        if not path.exists():
            logger.debug("no mcp_config.json at %s — running with zero MCP servers", path)
            return cls.empty()
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

        servers_raw = raw.get("mcpServers", {}) or {}
        servers: dict[str, MCPServerSpec] = {}
        for name, body in servers_raw.items():
            if not isinstance(body, dict):
                logger.warning("mcp_config: server %r is not a dict; skipping", name)
                continue
            spec = MCPServerSpec(
                name=name,
                command=str(body.get("command", "") or ""),
                args=tuple(body.get("args", []) or ()),
                env={k: _expandvars(str(v)) for k, v in (body.get("env") or {}).items()},
                url=str(body.get("url", "") or ""),
                max_tools=int(body.get("max_tools", 32) or 32),
            )
            try:
                spec.validate()
            except ValueError as exc:
                logger.warning("mcp_config: %s; skipping", exc)
                continue
            servers[name] = spec

        scopes_raw = raw.get("scopes", {}) or {}
        scopes: dict[str, list[str]] = {}
        for agent_name, server_list in scopes_raw.items():
            if not isinstance(server_list, list):
                continue
            scopes[str(agent_name)] = [str(s) for s in server_list]

        default_scope = [str(s) for s in (raw.get("default_scope", []) or []) if isinstance(s, str)]

        return cls(servers=servers, scopes=scopes, default_scope=default_scope)

    def servers_for(self, agent_name: str) -> list[str]:
        """Names of MCP servers whose tools should be attached to *agent_name*."""
        return list(self.scopes.get(agent_name, self.default_scope))


def _expandvars(value: str) -> str:
    """Expand ``$VAR`` / ``${VAR}`` in env values so users can reference secrets."""
    return os.path.expandvars(value)
