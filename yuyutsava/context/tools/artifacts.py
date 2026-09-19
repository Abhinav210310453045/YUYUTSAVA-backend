"""Context tools: reading back what compaction offloaded.

Offloaded tool results live in the artifact store; these read them back without re-running the tool that produced them.

Split out of the 242-line ``make_context_tools`` (`R-RDB-02`).
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool, tool

from yuyutsava.context.artifacts import (
    DEFAULT_LINE_COUNT,
    DEFAULT_SLICE_CHARS,
    ArtifactStore,
    clamp_slice_length,
)

logger = logging.getLogger("yuyutsava.context.tools.artifacts")


def make_artifacts_tools(store: ArtifactStore) -> list[BaseTool]:
    """Build the artifacts tools."""

    @tool
    async def ctx_fetch_artifact(
        artifact_id: str,
        offset: int = 0,
        length: int = DEFAULT_SLICE_CHARS,
        start_line: int | None = None,
        line_count: int = DEFAULT_LINE_COUNT,
    ) -> str:
        """Read a slice of an offloaded tool result. Nothing is ever lost.

        Large tool outputs are stored whole as artifacts; the tool result you
        saw carries the artifact_id plus a head/tail preview. Read the rest
        here, two ways:

        - by character: ``offset``/``length``, paging until the ``[more: …]``
          line stops appearing;
        - by line: ``start_line`` (1-based) with ``line_count`` — use this to
          read around a ctx_grep_artifact hit, whose output is
          ``<lineno>: <line>``.

        Prefer ctx_grep_artifact when you know what you are looking for. A
        single read is capped at 40,000 chars; ask again with the offset or
        line the footer reports to continue. The full body is always there.
        """
        if start_line is not None:
            ln = await store.read_lines(artifact_id, start_line=start_line, line_count=line_count)
            if ln is None:
                return f"[error] artifact {artifact_id!r} not found (expired or wrong id)"
            header = (
                f"[artifact {artifact_id} lines {ln.start_line}-{ln.end_line} of {ln.total_lines}]"
            )
            if ln.line_truncated:
                # Advancing to the next line here would skip the rest of THIS
                # line — the one continuation that would lose content.
                more = (
                    f"\n[line {ln.start_line} is longer than one read — read the "
                    f"rest with ctx_fetch_artifact(artifact_id, offset=…) "
                    f"character paging, not start_line]"
                )
            elif ln.end_line < ln.total_lines:
                more = (
                    f"\n[more: call ctx_fetch_artifact(artifact_id, start_line={ln.end_line + 1})]"
                )
            else:
                more = ""
            return f"{header}\n{ln.content}{more}"

        sl = await store.get(artifact_id, offset=offset, length=clamp_slice_length(length))
        if sl is None:
            return f"[error] artifact {artifact_id!r} not found (expired or wrong id)"
        end = sl.offset + len(sl.content)
        header = f"[artifact {artifact_id} chars {sl.offset}-{end} of {sl.total_chars}]"
        more = (
            f"\n[more: call ctx_fetch_artifact(artifact_id, offset={end})]"
            if end < sl.total_chars
            else ""
        )
        return f"{header}\n{sl.content}{more}"

    @tool
    async def ctx_grep_artifact(artifact_id: str, pattern: str, max_matches: int = 20) -> str:
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

    return [
        ctx_fetch_artifact,
        ctx_grep_artifact,
    ]
