"""ctx_* tools: read offloaded tool results back on demand.

These two tools are the retrieval half of the offload contract — every
digest the :class:`ToolResultOffloadMiddleware` injects names them in its
``hint`` field. Unlike the other prefixed tool families they are **always
visible** to the model (no ``tool_search`` discovery step): a digest is
useless if the model can't immediately act on it, so ``ctx_`` is *not* in
``ToolFilterMiddleware._SUPPRESS_PREFIXES``.

Responses are plain text with a one-line bracket header (not JSON) — the
payloads are large free-form bodies and JSON-escaping them only burns
tokens.
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool, tool

from yuyutsava.context.artifacts import (
    DEFAULT_SLICE_CHARS,
    ArtifactStore,
    thread_id_from_runtime,
)

logger = logging.getLogger("yuyutsava.context.tools")


def make_context_tools(store: ArtifactStore) -> list[BaseTool]:
    """Build the ctx_* tool pair bound to one artifact store."""

    @tool
    async def ctx_fetch_artifact(
        artifact_id: str, offset: int = 0, length: int = DEFAULT_SLICE_CHARS
    ) -> str:
        """Read a slice of an offloaded tool result.

        Large tool outputs are stored as artifacts; the tool result you saw
        contains the artifact_id plus a head/tail preview. Use this to read
        more, paging with ``offset``/``length`` (chars). Prefer
        ctx_grep_artifact when you are looking for something specific.
        """
        sl = await store.get(artifact_id, offset=offset, length=length)
        if sl is None:
            return f"[error] artifact {artifact_id!r} not found (expired or wrong id)"
        end = sl.offset + len(sl.content)
        header = (
            f"[artifact {artifact_id} chars {sl.offset}-{end} of {sl.total_chars}]"
        )
        more = (
            f"\n[more: call ctx_fetch_artifact(artifact_id, offset={end})]"
            if end < sl.total_chars
            else ""
        )
        return f"{header}\n{sl.content}{more}"

    @tool
    async def ctx_grep_artifact(
        artifact_id: str, pattern: str, max_matches: int = 20
    ) -> str:
        """Regex-search an offloaded tool result; returns matching lines.

        Much cheaper than paging through ctx_fetch_artifact when you know
        what you are looking for. ``pattern`` is a Python regex applied per
        line; output lines are ``<lineno>: <line>``.
        """
        matches = await store.grep(artifact_id, pattern, max_matches=max_matches)
        if matches is None:
            return f"[error] artifact {artifact_id!r} not found (expired or wrong id)"
        if not matches:
            return f"[artifact {artifact_id}] no lines matched {pattern!r}"
        body = "\n".join(matches)
        return f"[artifact {artifact_id} — {len(matches)} match(es) for {pattern!r}]\n{body}"

    tools: list[BaseTool] = [ctx_fetch_artifact, ctx_grep_artifact]

    # ctx_recall is only meaningful when the store maintains a semantic index
    # (Postgres + embedder). On SQLite the tool is simply not offered, so the
    # surface degrades cleanly rather than exposing a dead tool.
    if getattr(store, "supports_recall", False):

        @tool
        async def ctx_recall(query: str, k: int = 5) -> str:
            """Semantically recall relevant slices of earlier offloaded results.

            Searches everything offloaded in *this* conversation (web searches,
            large reads) for passages related to ``query`` and returns the best
            matches with their ``artifact_id`` and ``char_offset``. Use this when
            you need detail from an earlier tool result instead of re-running the
            tool; then ctx_fetch_artifact(artifact_id, offset=char_offset) for the
            full surrounding text.
            """
            hits = await store.recall(thread_id_from_runtime(), query, k=k)  # type: ignore[attr-defined]
            if not hits:
                return f"[no offloaded results matched {query!r}]"
            lines = [
                f"{i}. artifact={h.artifact_id} offset={h.char_offset} "
                f"score={h.score:.3f}\n   {h.snippet}"
                for i, h in enumerate(hits)
            ]
            return "[ctx_recall hits — fetch full text via ctx_fetch_artifact(artifact_id, offset)]\n" + "\n".join(lines)

        tools.append(ctx_recall)

    return tools
