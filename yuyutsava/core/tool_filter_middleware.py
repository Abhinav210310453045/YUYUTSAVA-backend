"""
Middleware that hides specific built-in tool schemas from the LLM.

The deepagents FilesystemMiddleware registers read_file, write_file, edit_file,
and execute alongside our tr_* equivalents.  Sending both sets to the LLM wastes
~700 tokens per call and risks the model choosing the unguarded versions.

This middleware intercepts wrap_model_call and strips the named tools from the
tool list before the request reaches the LLM.  The tools still exist in the
graph (so their ToolMessage handlers work), but the model never sees their
schemas and cannot call them.
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

# Suppress built-in deepagents tools the LLM should never call directly:
#   read_file / write_file / edit_file / execute  — replaced by tr_* equivalents
#   grep  — operates on virtual paths only; broken when given real absolute paths.
#            The LLM should use tr_grep instead, which shells out via the sandbox.
_SUPPRESS: frozenset[str] = frozenset({"read_file", "write_file", "edit_file", "execute", "grep"})


class ToolFilterMiddleware(AgentMiddleware[AgentState, Any, Any]):
    """Strip redundant built-in tool schemas before every LLM call."""

    def _filter(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        filtered = [
            t for t in request.tools
            if (t.name if hasattr(t, "name") else t.get("name")) not in _SUPPRESS
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
