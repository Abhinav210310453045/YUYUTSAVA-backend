"""
Middleware that hides specific tool schemas from the LLM.

Two suppression layers:

1. Named deepagents built-ins (read_file, write_file, edit_file, execute, grep)
   — replaced by tr_* equivalents; sending both wastes ~700 tokens and risks the
   model choosing the unguarded versions.

2. Our custom tool prefixes (tr_*, ws_*, sk_*, fo_*, ev_*)
   — these are injected into the graph for execution but hidden from the LLM's
   initial view. The agent discovers them on demand via tool_search('tr_*') etc.
   This is the ToolRegistry lazy-discovery pattern: only tool_search is visible
   upfront, all others are served on request to save context tokens.

Tools still exist in the graph (ToolMessage handlers work); the model just
never sees their schemas until it calls tool_search.
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
    "read_file", "write_file", "edit_file", "execute", "grep",
})

# Prefix suppression: all our custom-prefixed tools are hidden from the LLM
# upfront. The agent calls tool_search('tr_*') / tool_search('ws_*') to
# discover their schemas on demand.
_SUPPRESS_PREFIXES: tuple[str, ...] = ("tr_", "ws_", "sk_", "fo_", "ev_", "db_")


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
