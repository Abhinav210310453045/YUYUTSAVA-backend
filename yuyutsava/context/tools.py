"""ctx_* tools: read offloaded tool results and evicted turns back on demand.

Two retrieval halves, one promise: **nothing the agent has seen is ever
unreachable.**

* ``ctx_fetch_artifact`` / ``ctx_grep_artifact`` / ``ctx_recall`` read back
  tool results that :class:`~yuyutsava.context.offload_policy.ToolResultOffloadPolicy`
  moved out of state. Every digest it injects names them in its ``hint``.
* ``ctx_history`` / ``ctx_history_message`` / ``ctx_history_grep`` read back
  *turns* that :class:`~yuyutsava.context.compaction.YuyutsavaCompactionMiddleware`
  replaced with a summary. Compaction is the only step that removes messages
  from state; the verbatim messages survive in the transcript store, and these
  tools are the addressable path to them. Without this the summary is the only
  trace and detail is gone for good.
* ``ctx_compact`` is the agent-facing trigger for compaction (which otherwise
  fires only on the token threshold).

Unlike the other prefixed tool families these are **always visible** to the
model (no ``tool_search`` discovery step): a digest or a summary is useless if
the model cannot immediately act on it, so ``ctx_`` is *not* in
``ToolFilterPolicy``'s suppressed prefixes.

Every read is bounded by :data:`~yuyutsava.context.artifacts.MAX_SLICE_CHARS`
and ends in a ``[more: …]`` continuation line when there is more to read. The
bound is what keeps these tools exempt from offload safely: an unbounded read
would exceed ``LIMITS.max_tool_result_chars`` and be replaced by a "too large"
notice — the one shape of truncation that loses data instead of deferring it.

Responses are plain text with a one-line bracket header (not JSON) — the
payloads are large free-form bodies and JSON-escaping them only burns
tokens.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.tools import BaseTool, tool

from yuyutsava.context.artifacts import (
    DEFAULT_LINE_COUNT,
    DEFAULT_SLICE_CHARS,
    MAX_SLICE_CHARS,
    ArtifactStore,
    clamp_slice_length,
    thread_id_from_runtime,
)

logger = logging.getLogger("yuyutsava.context.tools")

# One ctx_history page. Kept modest because each entry is one clipped line —
# the agent widens with ctx_history_message(seq) where it matters.
DEFAULT_HISTORY_LIMIT = 40
MAX_HISTORY_LIMIT = 200

# Per-message clip inside a ctx_history listing.
HISTORY_ENTRY_CHARS = 600


def make_context_tools(
    store: ArtifactStore, transcript_store: Any | None = None
) -> list[BaseTool]:
    """Build the ctx_* tools bound to one artifact store.

    ``transcript_store`` is a
    :class:`~yuyutsava.context.transcript_store.TranscriptStore`; when it is
    supplied the ``ctx_history*`` trio is offered, giving the agent a verbatim
    read-back path for turns compaction has evicted. Without it the tools are
    simply not registered, so the surface degrades cleanly rather than
    exposing readers over a store that was never wired.
    """

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
            ln = await store.read_lines(
                artifact_id, start_line=start_line, line_count=line_count
            )
            if ln is None:
                return f"[error] artifact {artifact_id!r} not found (expired or wrong id)"
            header = (
                f"[artifact {artifact_id} lines {ln.start_line}-{ln.end_line} "
                f"of {ln.total_lines}]"
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
                    f"\n[more: call ctx_fetch_artifact(artifact_id, "
                    f"start_line={ln.end_line + 1})]"
                )
            else:
                more = ""
            return f"{header}\n{ln.content}{more}"

        sl = await store.get(
            artifact_id, offset=offset, length=clamp_slice_length(length)
        )
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

    @tool
    async def ctx_compact() -> str:
        """Compact this conversation's older turns into a summary now.

        Use when the context is getting long and the older turns are no
        longer needed verbatim — e.g. after finishing a subtask, or before
        starting something new in the same conversation. The summary keeps
        the session intent, decisions, and artifact ids; recent messages
        stay verbatim. Compaction runs automatically near the token budget,
        so only call this to compact EARLIER than that.
        """
        # Lazy import: keeps this module free of the middleware stack.
        from yuyutsava.context.compaction import request_compaction

        thread_id = thread_id_from_runtime()
        if not thread_id:
            return "[error] no active thread — compaction not scheduled"
        request_compaction(thread_id)
        return (
            "[ok] compaction scheduled — older turns will be summarized on "
            "the next model call in this thread"
        )

    tools: list[BaseTool] = [ctx_fetch_artifact, ctx_grep_artifact, ctx_compact]

    # ctx_history*: the read-back path for turns compaction has evicted.
    # Offered only when a transcript store is wired (it is on both backends;
    # the semantic *index* is Postgres-only, but these tools need only the
    # rows).
    if transcript_store is not None:

        @tool
        async def ctx_history(after_seq: int = 0, limit: int = DEFAULT_HISTORY_LIMIT) -> str:
            """List this conversation's messages verbatim, oldest first.

            When older turns have been compacted into a summary, this is where
            the originals still live — every message of this conversation is
            recorded as it happened. Use it to recover a detail the summary
            dropped: an exact id, path, number, filename, or what the user
            actually said.

            Each entry is one clipped line prefixed with ``#<seq>``; read a
            whole message with ctx_history_message(seq), or search instead
            with ctx_history_grep(pattern). Page with ``after_seq`` from the
            ``[more: …]`` footer.
            """
            recs = await _history_page(transcript_store, after_seq, limit)
            if recs is None:
                return "[error] no active thread — cannot read this conversation's history"
            if not recs:
                where = f" after #{after_seq}" if after_seq else ""
                return f"[history] no recorded messages{where}"
            lines = [_render_entry(r) for r in recs]
            last = recs[-1].seq
            header = (
                f"[history #{recs[0].seq}-{last} · {len(recs)} message(s) · "
                f"clipped to {HISTORY_ENTRY_CHARS} chars each]"
            )
            more = (
                f"\n[more: call ctx_history(after_seq={last})]"
                if len(recs) >= _history_limit(limit)
                else ""
            )
            return f"{header}\n" + "\n".join(lines) + more

        @tool
        async def ctx_history_message(
            seq: int, offset: int = 0, length: int = DEFAULT_SLICE_CHARS
        ) -> str:
            """Read one message of this conversation in full, by its ``#seq``.

            ``seq`` comes from a ctx_history listing or a ctx_history_grep hit.
            Returns the message verbatim — prose, tool call arguments, or a
            tool result — paged with ``offset``/``length`` exactly like
            ctx_fetch_artifact, so even a very large message is fully
            readable.
            """
            rec = await _history_one(transcript_store, seq)
            if rec is None:
                return (
                    f"[error] no message #{seq} in this conversation "
                    "(check the seq from ctx_history / ctx_history_grep)"
                )
            body = _render_full(rec)
            total = len(body)
            start = max(0, offset)
            content = body[start : start + clamp_slice_length(length)]
            end = start + len(content)
            header = (
                f"[history #{rec.seq} {rec.type} chars {start}-{end} of {total}]"
            )
            more = (
                f"\n[more: call ctx_history_message({seq}, offset={end})]"
                if end < total
                else ""
            )
            return f"{header}\n{content}{more}"

        @tool
        async def ctx_history_grep(pattern: str, max_matches: int = 20) -> str:
            """Regex-search this conversation's verbatim messages.

            The cheap way to recover something from turns that have been
            compacted away: search for the id, path or phrase instead of
            paging ctx_history. ``pattern`` is a Python regex applied per
            line; hits are ``#<seq> <type>: <line>``. Widen any hit with
            ctx_history_message(seq).
            """
            recs = await _history_page(transcript_store, 0, MAX_HISTORY_LIMIT)
            if recs is None:
                return "[error] no active thread — cannot read this conversation's history"
            try:
                rx = re.compile(pattern)
            except re.error as exc:
                return f"[error] invalid regex: {exc}"
            out: list[str] = []
            for rec in recs:
                for line in _render_full(rec).splitlines():
                    if rx.search(line):
                        out.append(f"#{rec.seq} {rec.type}: {line[:500]}")
                        if len(out) >= max(1, max_matches):
                            break
                if len(out) >= max(1, max_matches):
                    break
            if not out:
                return f"[history] no lines matched {pattern!r}"
            return (
                f"[history — {len(out)} match(es) for {pattern!r}; "
                f"widen with ctx_history_message(seq)]\n" + "\n".join(out)
            )

        tools.extend([ctx_history, ctx_history_message, ctx_history_grep])

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


# ---------------------------------------------------------------------------
# ctx_history helpers — transcript rows → readable text
# ---------------------------------------------------------------------------


def _history_limit(limit: int) -> int:
    return max(1, min(limit, MAX_HISTORY_LIMIT))


async def _history_page(store: Any, after_seq: int, limit: int) -> list[Any] | None:
    """One page of this thread's recorded messages. ``None`` = no live thread."""
    thread_id = thread_id_from_runtime()
    if not thread_id or thread_id == "unknown":
        return None
    try:
        return await store.list_messages(
            thread_id, after_seq=max(0, after_seq), limit=_history_limit(limit)
        )
    except Exception:  # noqa: BLE001 — a read-back failure must not fail the turn
        logger.exception("ctx_history: transcript read failed")
        return []


async def _history_one(store: Any, seq: int) -> Any | None:
    """The message at exactly ``seq``, or ``None``.

    ``seq`` is allocated by AUTOINCREMENT/BIGSERIAL and may have gaps, so
    ``after_seq=seq-1`` can hand back a *later* message; the equality check is
    what makes a bad seq an error rather than silently wrong content.
    """
    recs = await _history_page(store, max(0, seq - 1), 1)
    if not recs:
        return None
    return recs[0] if recs[0].seq == seq else None


def _msg_data(rec: Any) -> dict:
    """The ``message_to_dict`` payload of a transcript row."""
    content = getattr(rec, "content", None)
    if not isinstance(content, dict):
        return {}
    data = content.get("data")
    return data if isinstance(data, dict) else content


def _msg_text(data: dict) -> str:
    """Prose of a recorded message, flattened from str or block-list content."""
    # Lazy: core.streaming pulls in the graph/interrupt stack, and this module
    # is imported while the tool registry is being built.
    from yuyutsava.core.streaming import flatten_content

    return flatten_content(data.get("content")).strip()


def _tool_call_summary(data: dict, *, arg_chars: int) -> str:
    """``→ name(arg=…, arg=…)`` for each tool call on an AI message."""
    calls = data.get("tool_calls") or []
    if not isinstance(calls, list):
        return ""
    out: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("name") or "?"
        args = call.get("args")
        if isinstance(args, dict):
            rendered = ", ".join(
                f"{k}={_clip(json.dumps(v) if not isinstance(v, str) else v, arg_chars)}"
                for k, v in args.items()
            )
        else:
            rendered = _clip(str(args or ""), arg_chars)
        out.append(f"→ {name}({rendered})")
    return " ".join(out)


def _clip(text: str, limit: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _render_entry(rec: Any) -> str:
    """One clipped line for a ctx_history listing."""
    data = _msg_data(rec)
    kind = getattr(rec, "type", "?")
    if kind == "tool":
        name = data.get("name") or "tool"
        body = _clip(_msg_text(data) or str(data.get("content") or ""), HISTORY_ENTRY_CHARS)
        return f"#{rec.seq} tool ← {name}: {body}"
    text = _msg_text(data)
    if kind == "ai":
        summary = _tool_call_summary(data, arg_chars=120)
        body = _clip(text, HISTORY_ENTRY_CHARS) if text else ""
        joined = " ".join(p for p in (body, summary) if p)
        return f"#{rec.seq} ai {joined or '(no content)'}"
    return f"#{rec.seq} {kind} {_clip(text, HISTORY_ENTRY_CHARS) or '(no content)'}"


def _render_full(rec: Any) -> str:
    """A recorded message rendered verbatim, for reading or searching.

    Tool-call arguments are included in full: they are often the thing worth
    recovering (the script that was written, the artifact that was built), and
    ``ctx_history_message`` pages the result, so length is not a reason to
    withhold them.
    """
    data = _msg_data(rec)
    kind = getattr(rec, "type", "?")
    parts: list[str] = []
    text = _msg_text(data)
    if kind == "tool":
        name = data.get("name") or "tool"
        raw = text or str(data.get("content") or "")
        return f"tool result ← {name}\n{raw}"
    if text:
        parts.append(text)
    calls = data.get("tool_calls") or []
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            args = call.get("args")
            rendered = (
                json.dumps(args, indent=2, ensure_ascii=False)
                if isinstance(args, dict)
                else str(args or "")
            )
            parts.append(f"→ {call.get('name') or '?'}({rendered})")
    return "\n".join(parts) if parts else "(no content)"


__all__ = ["make_context_tools", "MAX_SLICE_CHARS"]
