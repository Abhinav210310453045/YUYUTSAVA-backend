"""Context tools: reading turns compaction has evicted.

The read-back path for a compacted conversation. Without these a turn that has been evicted from the window is simply gone, which is the difference between compaction and data loss.

Split out of the 242-line ``make_context_tools`` (`R-RDB-02`).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.tools import BaseTool, tool

from yuyutsava.context.artifacts import DEFAULT_SLICE_CHARS, clamp_slice_length
from yuyutsava.run_context import thread_id_from_runtime
from yuyutsava.text import flatten_content

logger = logging.getLogger("yuyutsava.context.tools.history")


# One ctx_history page. Kept modest because each entry is one clipped line —
# the agent widens with ctx_history_message(seq) where it matters.
DEFAULT_HISTORY_LIMIT = 40
MAX_HISTORY_LIMIT = 200

# Per-message clip inside a ctx_history listing.
HISTORY_ENTRY_CHARS = 600


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


def make_history_tools(transcript_store: Any) -> list[BaseTool]:
    """Build the history tools."""

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
        header = f"[history #{rec.seq} {rec.type} chars {start}-{end} of {total}]"
        more = f"\n[more: call ctx_history_message({seq}, offset={end})]" if end < total else ""
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

    return [
        ctx_history,
        ctx_history_message,
        ctx_history_grep,
    ]
