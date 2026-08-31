"""
ToolRegistry — progressive tool discovery for YUYUTSAVA agents.

The registry holds all available tools but keeps their full schemas out of the
LLM context until the agent actually needs one. It is a thin adapter over the
shared ``yuyutsava.discovery`` layer:

  Tier-0  ``catalog_block()``  — cheap, always-visible ``name: blurb`` list
          (injected into the system prompt) so the model knows what exists.
  Tier-1  ``tool_search(...)`` — ``select:name`` exact fetch or a bounded,
          ranked keyword search. A bare ``*`` returns the catalog, never the
          full schemas — the old wildcard dump is gone.
  Tier-2  full JSON schema — materialised one matched tool at a time on expand.

Tool naming convention (enforced by callers, not the registry):
  tr_*   — TaskRunner tools (read, write, delete, execute, execute_in_sandbox, grep, ask_user)
  ws_*   — Web search tools (ws_tavily_search, ws_exa_search, ws_exa_get_contents)
  fo_*   — FileOrganizer domain tools (fo_fetch_event)
  sk_*   — Skills tools (sk_read_skill, sk_write_skill, sk_search_skill)
  ev_*   — Event tools (ev_recall)
  orch_* — Orchestrator tools (orch_dispatch, orch_ask_user)

Usage:
    registry = ToolRegistry()
    registry.register_many(make_search_tools(search_config))

    # Inject only tool_search at startup; its catalog block goes in the prompt.
    startup_tools = [registry.make_tool_search_tool()]
    system_prompt += registry.catalog_block()
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import BaseTool

from yuyutsava.discovery import (
    CatalogEntry,
    KeywordCatalogProvider,
    make_discovery_search_tool,
)

logger = logging.getLogger("yuyutsava.core.tool_registry")

_BLURB_CHARS = 80


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

    def to_catalog(self) -> KeywordCatalogProvider:
        """Build the discovery provider over the registered tools."""
        entries: list[CatalogEntry] = []
        for t in self.all_tools():
            desc = (t.description or "").strip()
            blurb = desc.splitlines()[0] if desc else t.name
            if len(blurb) > _BLURB_CHARS:
                blurb = blurb[: _BLURB_CHARS - 1].rstrip() + "…"
            group = t.name.split("_", 1)[0] if "_" in t.name else "misc"
            entries.append(
                CatalogEntry(
                    id=t.name,
                    group=group,
                    blurb=blurb,
                    match_text=f"{t.name} {desc}",
                    load_detail=lambda t=t: self.schema_block([t]),
                )
            )
        return KeywordCatalogProvider(entries)

    def catalog_block(self) -> str:
        """Tier-0 always-visible ``name: blurb`` catalog for the system prompt."""
        return self.to_catalog().catalog_block() or ""

    def make_tool_search_tool(self) -> BaseTool:
        """Return a tool_search tool over this registry's catalog."""
        examples = (
            "Examples:\n"
            "  tool_search('select:tr_write_file')   — load one tool you know by name\n"
            "  tool_search('run a shell command')    — find a tool by what it does\n"
            "  tool_search('tr_*')                   — narrow by namespace (capped)\n"
            "Read the returned schema before calling — do NOT guess parameters."
        )
        return make_discovery_search_tool(
            self.to_catalog(), name="tool_search", noun="tool", examples=examples
        )
