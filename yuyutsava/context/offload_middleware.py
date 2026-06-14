"""Offload oversized tool results to the artifact store — before state.

This is the root-cause fix for context rot. The old path
(``core/streaming.py`` → ``guard_tool_result``) mutated the *streamed copy*
of a ToolMessage after the checkpointer had already persisted the full
blob; it protected the display, not the context. This middleware wraps tool
execution itself (``awrap_tool_call``), so the digest is what enters graph
state, the checkpoint, and every later model call. ``guard_tool_result``
stays in place as a display-side backstop for non-wrapped paths.

The digest is structured JSON the model can act on::

    {"offloaded": true, "artifact_id": "art_…", "tool": "ws_exa_search",
     "size_chars": 84211, "head": "<first 1500 chars>", "tail": "<last 500>",
     "hint": "Full output stored. Use ctx_fetch_artifact(...) or
              ctx_grep_artifact(...) to read more."}
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage

from yuyutsava.context.artifacts import ArtifactStore
from yuyutsava.context.config import ContextSettings

logger = logging.getLogger("yuyutsava.context.offload")

_HEAD_CHARS = 1_500
_TAIL_CHARS = 500

# Tools whose output must never be offloaded: the ctx_* readers themselves
# (offloading a fetch would loop), and small structured built-ins.
DEFAULT_EXCLUDE_TOOLS: frozenset[str] = frozenset({
    "ctx_fetch_artifact",
    "ctx_grep_artifact",
    "tool_search",
    "write_todos",
    "task",
})


def _thread_id_from_runtime() -> str:
    """Best-effort thread id from the active LangGraph run config."""
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        return str(cfg.get("configurable", {}).get("thread_id", "") or "unknown")
    except Exception:
        return "unknown"


class ToolResultOffloadMiddleware(AgentMiddleware):
    """Replace oversized ToolMessage content with an artifact digest."""

    def __init__(
        self,
        store: ArtifactStore,
        settings: ContextSettings,
        *,
        exclude_tools: frozenset[str] = DEFAULT_EXCLUDE_TOOLS,
    ) -> None:
        super().__init__()
        self._store = store
        self._threshold = settings.offload_threshold_chars
        self._exclude = exclude_tools

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        result = await handler(request)
        if not isinstance(result, ToolMessage):
            return result  # Command and friends pass through untouched
        content = result.content
        if not isinstance(content, str) or len(content) <= self._threshold:
            return result
        tool_name = request.tool_call.get("name", "") or (result.name or "tool")
        if tool_name in self._exclude:
            return result

        try:
            artifact_id = await self._store.put(
                _thread_id_from_runtime(), tool_name, content
            )
        except Exception:
            # Storage failure must not fail the agent turn; the display-side
            # guard_tool_result backstop still caps truly pathological sizes.
            logger.exception("offload: artifact put failed for %s — passing through", tool_name)
            return result

        digest = json.dumps({
            "offloaded": True,
            "artifact_id": artifact_id,
            "tool": tool_name,
            "size_chars": len(content),
            "head": content[:_HEAD_CHARS],
            "tail": content[-_TAIL_CHARS:],
            "hint": (
                "Full output stored. Use ctx_fetch_artifact(artifact_id, offset, "
                "length) to page through it or ctx_grep_artifact(artifact_id, "
                "pattern) to search it."
            ),
        })
        logger.debug(
            "offload: %s result %d chars → %s (%d-char digest)",
            tool_name, len(content), artifact_id, len(digest),
        )
        return ToolMessage(
            content=digest,
            tool_call_id=result.tool_call_id,
            name=result.name,
            id=result.id,
            status=result.status,
        )
