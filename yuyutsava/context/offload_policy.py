"""Offload oversized tool results to the artifact store — before state.

Phase 4 step 4.4, third migration (was ``ToolResultOffloadMiddleware``), and the
first policy to use the ``after_tool`` hook.

This is the root-cause fix for context rot. The old path
(``core/streaming.py`` → ``guard_tool_result``) mutated the *streamed copy* of a
ToolMessage after the checkpointer had already persisted the full blob; it
protected the display, not the context. Rewriting the result here means the
digest is what enters graph state, the checkpoint, and every later model call.
``guard_tool_result`` stays in place as a display-side backstop for non-wrapped
paths.

The digest is structured JSON the model can act on::

    {"offloaded": true, "artifact_id": "art_…", "tool": "ws_exa_search",
     "size_chars": 84211, "head": "<first 1500 chars>", "tail": "<last 500>",
     "hint": "Full output stored. Use ctx_fetch_artifact(...) or
              ctx_grep_artifact(...) to read more."}

**On result types:** ``after_tool`` deals in the framework's own result
vocabulary. ADR-004 draws the boundary around our decisions, not around
``ToolMessage`` — wrapping message types was rejected as disproportionate
(Alternative C), and a policy whose entire job is rewriting a message has to
speak that language. Everything else here — when to offload, what the digest
says, that storage failure must never fail the turn — is ours and is now
testable without a graph.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from yuyutsava.context.artifacts import ArtifactStore, thread_id_from_runtime
from yuyutsava.context.config import ContextSettings
from yuyutsava.context.digests import build_digest
from yuyutsava.context.meter import note_offload
from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import ToolCall

logger = logging.getLogger("yuyutsava.context.offload")

# Tools whose output must NEVER be offloaded, at any size: the ctx_* readers
# themselves (offloading a read-back would loop — the digest would replace the
# very content the agent asked to see) plus write_todos, whose result is a
# small fixed structure.
#
# Safe only because every ctx_* reader bounds its own output to
# ``MAX_SLICE_CHARS`` (40k) and appends a ``[more: …]`` line, keeping it under
# ``LIMITS.max_tool_result_chars`` (100k) where ``guard_tool_result`` would
# otherwise replace the body with a notice. Adding a reader here without that
# bound would reintroduce unrecoverable truncation.
DEFAULT_EXCLUDE_TOOLS: frozenset[str] = frozenset({
    "ctx_fetch_artifact",
    "ctx_grep_artifact",
    "ctx_recall",
    "ctx_history",
    "ctx_history_message",
    "ctx_history_grep",
    "write_todos",
})

# Tools exempt from the *always-offload prefix* rule but still subject to the
# size threshold. ``task`` and ``tool_search`` results are normally small and
# worth keeping inline verbatim — but a subagent can return tens of thousands
# of characters, and these were previously exempt at every size, so an
# oversized one reached ``guard_tool_result`` and came back as a "too large"
# notice with the body nowhere. Size-offloading them instead stores the whole
# thing and hands the agent an artifact id.
SIZE_ONLY_TOOLS: frozenset[str] = frozenset({
    "tool_search",
    "task",
})


class ToolResultOffloadPolicy(Policy):
    """Replace oversized ToolMessage content with an artifact digest."""

    name = "ToolResultOffloadPolicy"

    def __init__(
        self,
        store: ArtifactStore,
        settings: ContextSettings,
        *,
        exclude_tools: frozenset[str] = DEFAULT_EXCLUDE_TOOLS,
        size_only_tools: frozenset[str] = SIZE_ONLY_TOOLS,
    ) -> None:
        super().__init__()
        self._store = store
        self._threshold = settings.offload_threshold_chars
        self._always_prefixes = tuple(settings.always_offload_prefixes)
        self._exclude = exclude_tools
        self._size_only = size_only_tools

    def _should_offload(self, tool_name: str, content: str) -> bool:
        """Offload when over the size threshold OR a reference-class tool.

        ``always_offload_prefixes`` (default ``("ws_",)``) forces offload of
        small-but-accumulating results (web search) regardless of size; every
        other tool keeps the original size-gated behaviour exactly.

        Tools in ``size_only_tools`` skip the prefix rule but keep the size
        check, so they stay inline when small and become a recoverable
        artifact when large.
        """
        if len(content) > self._threshold:
            return True
        if tool_name in self._size_only:
            return False
        return any(tool_name.startswith(p) for p in self._always_prefixes)

    async def after_tool(self, call: ToolCall, result: Any) -> Any:
        from langchain_core.messages import ToolMessage

        if not isinstance(result, ToolMessage):
            return result  # Command and friends pass through untouched
        content = result.content
        if not isinstance(content, str):
            return result
        tool_name = call.name or (result.name or "tool")
        if tool_name in self._exclude:
            return result
        if not self._should_offload(tool_name, content):
            return result

        thread_id = thread_id_from_runtime()
        try:
            artifact_id = await self._store.put(thread_id, tool_name, content)
        except Exception:
            # Storage failure must not fail the agent turn; the display-side
            # guard_tool_result backstop still caps truly pathological sizes.
            logger.exception(
                "offload: artifact put failed for %s — passing through", tool_name)
            return result

        digest = json.dumps(build_digest(tool_name, artifact_id, content))
        logger.debug(
            "offload: %s result %d chars → %s (%d-char digest)",
            tool_name, len(content), artifact_id, len(digest),
        )
        # How much this conversation kept OUT of the window is the counterpart
        # to how full the window is; one without the other reads as if the
        # offload machinery were doing nothing.
        note_offload(thread_id)
        return ToolMessage(
            content=digest,
            tool_call_id=result.tool_call_id,
            name=result.name,
            id=result.id,
            status=result.status,
        )


__all__ = ["DEFAULT_EXCLUDE_TOOLS", "SIZE_ONLY_TOOLS", "ToolResultOffloadPolicy"]
