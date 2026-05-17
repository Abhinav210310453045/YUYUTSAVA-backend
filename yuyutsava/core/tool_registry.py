"""
ToolRegistry — lazy tool discovery and schema serving for YUYUTSAVA agents.

The registry holds all available tools but only injects their full schemas
into the LLM context when the agent explicitly calls tool_search(pattern).
This avoids burning thousands of tokens on tool schemas before any work begins.

Tool naming convention (enforced by callers, not the registry):
  tr_*   — TaskRunner tools (read, write, delete, execute, execute_in_sandbox, grep, ask_user)
  ws_*   — Web search tools (ws_tavily_search, ws_exa_search, ws_exa_get_contents)
  fo_*   — FileOrganizer domain tools (fo_fetch_event)
  sk_*   — Skills tools (sk_read_skill, sk_write_skill)
  ev_*   — Event tools (ev_recall)
  orch_* — Orchestrator tools (orch_dispatch, orch_ask_user)

Usage:
    registry = ToolRegistry()
    registry.register_many(bind_tools(workspace))
    registry.register_many(make_search_tools(search_config))
    registry.register_many(make_skill_tools(skill_registry))

    # Inject only tool_search at agent startup — all others withheld
    startup_tools = [registry.make_tool_search_tool()]

    # Agent calls tool_search('tr_*') → gets schemas → calls tr_write_file
    # Agent calls tool_search('ws_*') → gets schemas → calls ws_tavily_search
"""

from __future__ import annotations

import fnmatch
import json
import logging
from typing import Any

from langchain_core.tools import BaseTool, tool

logger = logging.getLogger("yuyutsava.core.tool_registry")


class ToolRegistry:
    """Holds all tools; exposes them via a single tool_search gateway."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, t: BaseTool) -> None:
        self._tools[t.name] = t

    def register_many(self, tools: list[BaseTool]) -> None:
        for t in tools:
            self.register(t)

    def all_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def search(self, pattern: str) -> list[BaseTool]:
        """Return tools whose names match the fnmatch pattern."""
        pat = pattern.strip()
        if not pat or pat == "*":
            return list(self._tools.values())
        return [t for name, t in self._tools.items() if fnmatch.fnmatchcase(name, pat)]

    def schema_block(self, tools: list[BaseTool]) -> str:
        """Render a compact JSON schema block for the given tools."""
        schemas: list[dict[str, Any]] = []
        for t in tools:
            try:
                schema = t.args_schema.model_json_schema() if t.args_schema else {}
            except Exception:
                schema = {}
            schemas.append({
                "name": t.name,
                "description": (t.description or "").strip(),
                "parameters": schema,
            })
        return json.dumps(schemas, indent=2)

    def make_tool_search_tool(self) -> BaseTool:
        """Return a tool_search tool bound to this registry."""
        registry = self

        @tool
        def tool_search(pattern: str) -> str:
            """Search available tools by name pattern (supports * wildcards).

            Returns each matching tool's name, description, and JSON parameter
            schema so you can call it correctly.

            Examples:
              tool_search('tr_*')        — all TaskRunner tools
              tool_search('ws_*')        — all web search tools
              tool_search('sk_*')        — all Skills tools
              tool_search('tr_execute')  — tool to execute out of sandbox, permission-gated shell (network access)
              tool_search('tr_write*')   — just write/delete tools
              tool_search('*')           — everything (expensive, avoid)

            Call this before using a tool you haven't used yet in this task.
            """
            matches = registry.search(pattern)
            if not matches:
                return f"no tools matched pattern {pattern!r}. Try tool_search('*') to see all."
            result = registry.schema_block(matches)
            logger.debug("tool_search(%r) → %d matches", pattern, len(matches))
            return result

        return tool_search
