"""Reading what a LangGraph host says, and saying it back in one line.

Pure functions over the shapes the Agent Protocol returns — run records, thread
values, message lists. They are separate from the watcher because the SDK's
shapes are a moving target: a run from ``runs.list`` frequently omits fields the
same run carries when fetched by id, and objects arrive sometimes as dicts and
sometimes as models, so every reader here has to tolerate both. None of that is
about watching tasks.
"""

from __future__ import annotations

import json
from typing import Any

from yuyutsava.async_subagents.mirror import TERMINAL_STATUSES

# Statuses we treat as "still running" for HITL/polling purposes.
NON_TERMINAL = frozenset({"pending", "running"})


def first_str(*parts: str | None) -> str:
    for p in parts:
        if p:
            return p
    return ""


def clean_agent_name(raw: str | None) -> str | None:
    """Display name from a graph/assistant id (strips the async ``-bg`` suffix)."""
    if not raw or not isinstance(raw, str):
        return None
    name = raw.strip()
    if name.endswith("-bg"):
        name = name[:-3]
    return name or None


def extract_thread_id(thread_obj: Any) -> str | None:
    if isinstance(thread_obj, dict):
        return thread_obj.get("thread_id")
    return getattr(thread_obj, "thread_id", None)


def first_run_status(runs: Any) -> str:
    """Status of the latest (newest-first) run, or '' when there are none."""
    if not runs:
        return ""
    r = runs[0]
    if isinstance(r, dict):
        return r.get("status") or ""
    return getattr(r, "status", "") or ""


def last_message_text(values: Any) -> str:
    """Best-effort extraction of the final assistant message text.

    Thread values come back as a dict with ``messages: list[Message]``.
    Each message may be a dict (``{"role": ..., "content": ...}``) or an
    object with ``.content``. Content may be a string or a list of blocks.
    """
    if not isinstance(values, dict):
        return ""
    msgs = values.get("messages") or []
    if not msgs:
        return ""
    last = msgs[-1]
    content = last.get("content") if isinstance(last, dict) else getattr(last, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                out.append(str(block["text"]))
            elif isinstance(block, str):
                out.append(block)
        return "\n".join(out)
    return str(content) if content is not None else ""


def content_text(content: Any) -> str:
    """Flatten a message ``content`` (str | list-of-blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                out.append(str(block["text"]))
            elif isinstance(block, str):
                out.append(block)
        return "\n".join(out)
    return str(content) if content is not None else ""


_COMPACT_ERROR_CAP = 300


def compact_error(raw: str | None) -> str:
    """Reduce a possibly-huge error/traceback to a short, single-line reason.

    Background-task errors get injected into the launching agent's context, so a
    multi-line traceback would bloat it. We keep only the most meaningful line —
    the final ``Type: message`` of a Python traceback, else the last non-empty
    line — and hard-cap the length. The full text stays available via the
    per-task logs endpoint.
    """
    if not raw:
        return "(no error detail)"
    text = str(raw).strip()
    if not text:
        return "(no error detail)"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    chosen = text
    if lines:
        # A Python traceback ends with the exception line ("ValueError: ...").
        # That is the most useful single line; fall back to the last line.
        for ln in reversed(lines):
            if not ln.startswith(('File "', "Traceback", "  ", "during", "During")):
                chosen = ln
                break
        else:
            chosen = lines[-1]
    chosen = " ".join(chosen.split())
    if len(chosen) > _COMPACT_ERROR_CAP:
        chosen = chosen[: _COMPACT_ERROR_CAP - 1].rstrip() + "…"
    return chosen


def run_error_text(run: Any) -> str:
    """Extract an error string from a run object across SDK shapes.

    langgraph-api serializes a run failure as
    ``{"error": <ExceptionType>, "message": <detail>}`` and masks the message to
    ``"An internal error occurred"`` for any exception type outside its allow-list
    (serde.py). The exception *type* survives that masking and is often the only
    diagnostic clue — ``RateLimitError`` vs ``TimeoutError`` vs a real bug — so we
    surface both type and message rather than the message alone.
    """
    if isinstance(run, dict):
        err = run.get("error")
    else:
        err = getattr(run, "error", None)
    if not err:
        return ""
    if isinstance(err, dict):
        etype = str(err.get("error") or "").strip()
        msg = str(err.get("message") or "").strip()
        if etype and msg:
            return f"{etype}: {msg}"
        return etype or msg
    return str(err).strip()


def tool_message_is_error(status: Any, content: Any) -> bool:
    """Whether a ToolMessage genuinely represents a failure.

    The message-level ``status == "error"`` is authoritative. Otherwise, if the
    content is a JSON tool envelope, trust its own ``status``/``error`` fields: a
    successful ``{"status": "success", "error": null}`` result must NOT count as
    an error merely because the literal word ``"error"`` appears as a key. Only
    non-JSON content falls back to a traceback/exception keyword sniff.
    """
    if status == "error":
        return True
    text = content_text(content).strip()
    if not text:
        return False
    try:
        envelope = json.loads(text)
    except (ValueError, TypeError):
        envelope = None
    if isinstance(envelope, dict):
        if str(envelope.get("status", "")).lower() == "error":
            return True
        if envelope.get("error"):
            return True
        # A well-formed envelope that declares its own status/error is trusted:
        # having reached here it did not fail, so it is a success, not an error.
        if "status" in envelope or "error" in envelope:
            return False
    low = text.lower()
    return any(kw in low for kw in ("traceback (most recent call last)", "exception:", "error:"))


def error_text_from_thread(values: Any) -> str:
    """Best-effort error reason from a failed sub-thread's messages.

    Used when the run object carries no ``error`` field. Returns the most recent
    ToolMessage that *genuinely* failed, or "" when none is found — so the caller
    can substitute a clear generic reason instead of mislabeling a benign success
    payload or completion summary as the error.
    """
    if not isinstance(values, dict):
        return ""
    msgs = values.get("messages") or []
    for m in reversed(msgs):
        mtype = m.get("type") or m.get("role") if isinstance(m, dict) else getattr(m, "type", "")
        status = m.get("status") if isinstance(m, dict) else getattr(m, "status", None)
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if mtype == "tool" and tool_message_is_error(status, content):
            txt = content_text(content).strip()
            if txt:
                return txt
    return ""


def transcript_rows(values: Any) -> list[dict]:
    """Flatten a sub-thread's ``messages`` into UI-friendly transcript rows.

    Each row is ``{role, text, tool_name?, tool_args?, status?}``. Tool calls
    surface their name + args; tool results and assistant/human text surface
    their flattened content. Shares ``content_text`` with the live progress
    stream so the on-demand transcript and the streamed steps agree.
    """
    if not isinstance(values, dict):
        return []
    rows: list[dict] = []
    for m in values.get("messages") or []:
        if isinstance(m, dict):
            mtype = m.get("type") or m.get("role") or ""
            content = m.get("content", "")
            tcs = m.get("tool_calls")
            status = m.get("status")
            name = m.get("name")
        else:
            mtype = getattr(m, "type", "") or ""
            content = getattr(m, "content", "")
            tcs = getattr(m, "tool_calls", None)
            status = getattr(m, "status", None)
            name = getattr(m, "name", None)
        role = (
            "assistant"
            if mtype in ("ai", "assistant")
            else ("user" if mtype in ("human", "user") else (mtype or "message"))
        )
        text = content_text(content).strip()
        if text:
            rows.append({"role": role, "text": text})
        for tc in tcs or []:
            tc_name = tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
            tc_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            try:
                args_str = json.dumps(tc_args, ensure_ascii=False) if tc_args else ""
            except (TypeError, ValueError):
                args_str = str(tc_args)
            rows.append(
                {"role": "tool_call", "tool_name": tc_name, "tool_args": args_str, "text": ""}
            )
        if mtype == "tool" and text:
            rows[-1]["role"] = "tool_result"
            if name:
                rows[-1]["tool_name"] = name
            if status:
                rows[-1]["status"] = status
    return rows


def new_progress_steps(values: Any, seen: int) -> tuple[list[tuple[str, str]], int]:
    """Diff a sub-thread's message history into new progress steps.

    Returns ``(steps, total)`` where ``steps`` is a list of
    ``(kind_hint, text)`` for each message past ``seen`` — a tool call
    (``"tool_call"``, ``"<name> <args>"``) or a chunk of assistant text
    (``"text"``). Tool results and human turns are skipped to keep the live
    stream readable; the full payload is always available via the event's copy
    button on the UI side. ``total`` is the new high-water message count.
    """
    if not isinstance(values, dict):
        return [], seen
    msgs = values.get("messages") or []
    total = len(msgs)
    if total <= seen:
        return [], total
    steps: list[tuple[str, str]] = []
    for m in msgs[seen:]:
        if isinstance(m, dict):
            tcs = m.get("tool_calls")
            content = m.get("content", "")
            mtype = m.get("type") or m.get("role") or ""
        else:
            tcs = getattr(m, "tool_calls", None)
            content = getattr(m, "content", "")
            mtype = getattr(m, "type", "") or ""
        if tcs:
            for tc in tcs:
                name = tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
                args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                try:
                    args_str = json.dumps(args, ensure_ascii=False) if args else ""
                except (TypeError, ValueError):
                    args_str = str(args)
                steps.append(("tool_call", f"{name} {args_str}".strip()[:200]))
        elif mtype in ("ai", "assistant"):
            text = content_text(content).strip()
            if text:
                steps.append(("text", text[:200]))
    return steps, total


__all__ = [
    "NON_TERMINAL",
    "TERMINAL_STATUSES",
    "clean_agent_name",
    "compact_error",
    "error_text_from_thread",
    "extract_thread_id",
    "first_run_status",
    "last_message_text",
    "new_progress_steps",
    "run_error_text",
    "transcript_rows",
]
