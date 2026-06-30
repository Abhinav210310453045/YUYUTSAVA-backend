"""
Middleware that hides specific tool schemas from the LLM.

Two suppression layers:

1. Named deepagents built-ins (read_file, write_file, edit_file, execute, grep,
   ls, glob) — replaced by tr_* equivalents; sending both wastes ~700 tokens
   and risks the model choosing the unguarded versions. ls/glob are filtered
   because their virtual-path mode silently returns [] for any real path
   outside the workspace (see tr_ls / tr_glob, which are zone-checked).

2. Our custom tool prefixes (tr_*, ws_*, sk_*, fo_*, ev_*, db_*, mem_*)
   — these are injected into the graph for execution but hidden from the LLM's
   initial view. The model sees their NAMES in the always-visible catalog
   (built by ToolRegistry.catalog_block, injected into the system prompt) and
   loads a schema on demand via tool_search('select:<name>') or a keyword
   search. This is the ToolRegistry progressive-discovery pattern: names are
   cheap and always shown, full schemas are served on request to save tokens.

Tools still exist in the graph (ToolMessage handlers work); the model just
never sees their schemas until it pulls them with tool_search.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)

# Exact-name suppression: deepagents built-in tools replaced by tr_* equivalents.
_SUPPRESS_NAMES: frozenset[str] = frozenset({
    "read_file", "write_file", "edit_file", "execute", "grep", "ls", "glob",
})

# Prefix suppression: all our custom-prefixed tools are hidden from the LLM
# upfront. Their names stay visible in the system-prompt catalog; the agent
# pulls a schema on demand via tool_search('select:<name>') or a keyword search.
#
# ctx_* is deliberately NOT here: offload digests reference
# ctx_fetch_artifact / ctx_grep_artifact directly, so the model must always
# see their schemas (same always-visible treatment as tool_search itself).
_SUPPRESS_PREFIXES: tuple[str, ...] = ("tr_", "ws_", "sk_", "fo_", "ev_", "db_", "mem_")


def _should_suppress(name: str) -> bool:
    if name in _SUPPRESS_NAMES:
        return True
    return name.startswith(_SUPPRESS_PREFIXES)


class ToolFilterMiddleware(AgentMiddleware[AgentState, Any, Any]):
    """Strip redundant and lazy-discovery tool schemas before every LLM call."""

    def _filter(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        filtered = [
            t for t in request.tools
            if not _should_suppress(t.name if hasattr(t, "name") else t.get("name", ""))
        ]
        if len(filtered) == len(request.tools):
            return request
        return request.override(tools=filtered)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self._filter(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(self._filter(request))
