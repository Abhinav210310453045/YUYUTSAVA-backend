"""MCP config dataclasses + the two-level loader.

Two files, one schema (it mirrors Claude Code's so configs can be pasted)::

    ~/.yuyutsava/mcp_config.json           global    — every workspace
    <ws>/.yuyutsava/mcp_config.json        workspace — this project only

    {
      "mcpServers": {
        "filesystem":  {"command": "npx", "args": ["-y", "@.../server-filesystem", "~/Documents"]},
        "github":      {"command": "npx", "args": ["-y", "@.../server-github"], "env": {"GITHUB_TOKEN": "$GITHUB_TOKEN"}},
        "spotify":     {"url": "http://localhost:8765/mcp"}
      },
      "scopes": {
        "orchestrator":   ["spotify"],
        "file-organizer": ["filesystem"]
      },
      "default_scope": [],
      "trusted_workspaces": ["/abs/path/to/a/project"]      ← global file only
    }

- ``mcpServers``: name → either stdio (``command``, ``args``, ``env``) or
  SSE (``url``). ``$VAR`` / ``${VAR}`` expand in ``command``, every ``args``
  item, ``url`` and ``env`` values; ``${YUYUTSAVA_WORKSPACE}`` is the workspace
  root the config was loaded for, so a workspace file can point at its own
  scripts.
- ``scopes``: agent-name → list of server names whose tools that agent
  receives. Agents missing from ``scopes`` receive ``default_scope``. The
  masters are ``"orchestrator"`` (daemon), ``"cli"`` (chat/voice, standalone
  CLI) and ``"tinker"``; subagents use their ``BaseSubAgent.name``.
- ``trusted_workspaces``: a workspace file spawns processes at boot, so it is
  honoured only for workspaces listed here (absolute paths; ``~`` allowed).
  Anything else is logged and skipped.

:meth:`MCPConfig.load` merges the two: a workspace server overrides a global
one of the same name; ``scopes`` and ``default_scope`` are unioned per agent.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from yuyutsava.storage.paths import WorkspaceLayout, state_dir

logger = logging.getLogger("yuyutsava.mcp.config")

GLOBAL_CONFIG_NAME = "mcp_config.json"
WORKSPACE_VAR = "YUYUTSAVA_WORKSPACE"


def global_mcp_config_path() -> Path:
    """``~/.yuyutsava/mcp_config.json`` (honours ``YUYUTSAVA_HOME``)."""
    return state_dir() / GLOBAL_CONFIG_NAME


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
    # Which file defined it — for logs only. Excluded from equality so
    # MCPClientManager.hot_reload's spec diff never restarts a server just
    # because the same entry now comes from another file.
    source: str = field(default="", compare=False)

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
    """A loaded (or merged) ``mcp_config.json``."""

    servers: dict[str, MCPServerSpec]
    scopes: dict[str, list[str]]
    default_scope: list[str]
    # Global file only: workspaces whose own mcp_config.json is honoured.
    trusted_workspaces: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> MCPConfig:
        return cls(servers={}, scopes={}, default_scope=[])

    @classmethod
    def from_file(cls, path: Path | None = None, *, workspace: Path | None = None) -> MCPConfig:
        """Load one file; return :meth:`empty` if it is absent.

        *path* defaults to the global file. *workspace* only feeds the
        ``${YUYUTSAVA_WORKSPACE}`` expansion — it does NOT pick the workspace
        file; :meth:`load` does that.
        """
        if path is None:
            path = global_mcp_config_path()
        if not path.exists():
            logger.debug("no %s at %s — zero MCP servers from it", GLOBAL_CONFIG_NAME, path)
            return cls.empty()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

        expand = _expander(workspace)
        servers_raw = raw.get("mcpServers", {}) or {}
        servers: dict[str, MCPServerSpec] = {}
        for name, body in servers_raw.items():
            if not isinstance(body, dict):
                logger.warning("mcp_config %s: server %r is not a dict; skipping", path, name)
                continue
            spec = MCPServerSpec(
                name=name,
                command=expand(str(body.get("command", "") or "")),
                args=tuple(expand(str(a)) for a in (body.get("args", []) or ())),
                env={k: expand(str(v)) for k, v in (body.get("env") or {}).items()},
                url=expand(str(body.get("url", "") or "")),
                max_tools=int(body.get("max_tools", 32) or 32),
                source=str(path),
            )
            try:
                spec.validate()
            except ValueError as exc:
                logger.warning("mcp_config %s: %s; skipping", path, exc)
                continue
            servers[name] = spec

        scopes_raw = raw.get("scopes", {}) or {}
        scopes: dict[str, list[str]] = {}
        for agent_name, server_list in scopes_raw.items():
            if not isinstance(server_list, list):
                continue
            scopes[str(agent_name)] = [str(s) for s in server_list]

        default_scope = [str(s) for s in (raw.get("default_scope", []) or []) if isinstance(s, str)]
        trusted = tuple(
            str(t) for t in (raw.get("trusted_workspaces", []) or []) if isinstance(t, str)
        )
        return cls(
            servers=servers, scopes=scopes, default_scope=default_scope,
            trusted_workspaces=trusted,
        )

    @staticmethod
    def merge(base: MCPConfig, overlay: MCPConfig) -> MCPConfig:
        """*overlay* (workspace) on top of *base* (global).

        Servers: overlay wins on the same name. Scopes: per-agent ordered
        union. ``default_scope``: ordered union. ``trusted_workspaces`` is a
        global-only key and always comes from *base*.
        """
        servers = dict(base.servers)
        servers.update(overlay.servers)
        scopes = {agent: list(names) for agent, names in base.scopes.items()}
        for agent, names in overlay.scopes.items():
            merged = scopes.setdefault(agent, [])
            merged.extend(n for n in names if n not in merged)
        default_scope = list(base.default_scope)
        default_scope.extend(n for n in overlay.default_scope if n not in default_scope)
        return MCPConfig(
            servers=servers, scopes=scopes, default_scope=default_scope,
            trusted_workspaces=base.trusted_workspaces,
        )

    @classmethod
    def load(cls, workspace: Path | None) -> MCPConfig:
        """The effective config for *workspace*: global merged with the
        workspace's own ``.yuyutsava/mcp_config.json`` — when trusted."""
        cfg = cls.from_file(workspace=workspace)
        if workspace is None:
            return cfg
        ws_path = WorkspaceLayout.for_workspace(workspace).mcp_config
        if not ws_path.exists():
            return cfg
        if not cfg.trusts(workspace):
            logger.warning(
                "mcp: %s ignored — add %r to \"trusted_workspaces\" in %s to use it",
                ws_path, str(Path(workspace).expanduser().resolve()), global_mcp_config_path(),
            )
            return cfg
        overlay = cls.from_file(ws_path, workspace=workspace)
        logger.info("mcp: merged workspace config %s (%d server(s))", ws_path, len(overlay.servers))
        return cls.merge(cfg, overlay)

    def trusts(self, workspace: Path | str) -> bool:
        """True when *workspace* is listed in ``trusted_workspaces``."""
        target = _norm(workspace)
        return any(_norm(t) == target for t in self.trusted_workspaces)

    def servers_for(self, agent_name: str) -> list[str]:
        """Names of MCP servers whose tools should be attached to *agent_name*."""
        return list(self.scopes.get(agent_name, self.default_scope))


def _norm(p: Path | str) -> str:
    """Case/separator-normalized absolute path for trust comparisons."""
    return os.path.normcase(str(Path(p).expanduser().resolve()))


def _expander(workspace: Path | None) -> Callable[[str], str]:
    """``${YUYUTSAVA_WORKSPACE}`` / ``$YUYUTSAVA_WORKSPACE`` → the workspace
    root, then ``$VAR`` / ``${VAR}`` from the environment (unknown names are
    left as-is, like ``os.path.expandvars``)."""
    ws = str(Path(workspace).expanduser().resolve()) if workspace is not None else None

    def expand(value: str) -> str:
        if ws is not None:
            value = value.replace("${" + WORKSPACE_VAR + "}", ws).replace("$" + WORKSPACE_VAR, ws)
        return os.path.expandvars(value)

    return expand


__all__ = ["MCPConfig", "MCPServerSpec", "global_mcp_config_path"]
