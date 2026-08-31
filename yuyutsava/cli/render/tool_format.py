"""Smart one-line summaries for tool calls and results.

Pure stdlib functions (fast to test standalone). The idea comes from
deepagents-cli's ``tool_display.py``: show the *one* argument a human
cares about per tool (``tr_read_file(path)``, ``tr_execute(cmd…)``)
instead of a full arg dump, and compress results to a short outcome
("3 matches", "exit 0", the error message).

Shared error/pretty helpers used by both the rich and plain renderers
also live here.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_CALL_LEN = 120
MAX_RESULT_LEN = 100

# Zero-width / bidi-control characters that can visually spoof terminal
# output (same class of stripping deepagents-cli does), plus C0 controls.
_DANGEROUS = re.compile(
    "[\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069\\ufeff"
    "\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f]"
)


def sanitize(value: Any) -> str:
    """Flatten to a single spoof-safe display line."""
    s = str(value)
    s = _DANGEROUS.sub("", s)
    return " ".join(s.split())


def truncate(s: str, limit: int = MAX_CALL_LEN) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Call summaries
# ---------------------------------------------------------------------------

# Tool name → the args (in priority order) whose value IS the summary.
_CALL_KEYS: dict[str, tuple[str, ...]] = {
    "tr_read_file": ("path", "file_path"),
    "tr_write_file": ("path", "file_path"),
    "tr_delete_file": ("path", "file_path"),
    "tr_chmod": ("path", "file_path"),
    "tr_ls": ("path", "directory"),
    "tr_glob": ("pattern",),
    "tr_grep": ("pattern", "query"),
    "tr_execute": ("command",),
    "tr_execute_in_sandbox": ("command",),
    "tr_run_python": ("code", "script"),
    "tr_fetch_url": ("url",),
    "tr_ask_user": ("question", "prompt"),
    "task": ("description", "task", "prompt"),
    "start_async_task": ("description", "task", "prompt"),
}

# Prefix fallbacks when the exact name isn't registered.
_PREFIX_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ws_", ("query", "q")),
    ("doc_", ("query", "path")),
    ("sk_", ("query", "name", "skill")),
    ("vis_", ("title", "kind", "name")),
    ("artifact_", ("title", "name")),
    ("um_", ("query", "content")),
)


def _scope_suffix(name: str, args: dict) -> str:
    """`` in <path>`` suffix for search tools scoped to a non-default dir."""
    if name not in ("tr_grep", "tr_glob"):
        return ""
    path = args.get("path") or args.get("directory")
    if not isinstance(path, str) or path.strip() in ("", "/", "."):
        return ""
    return f" in {sanitize(path)}"


def format_call(name: str, args: Any) -> str:
    """One-line human summary of a tool call's arguments."""
    if not isinstance(args, dict):
        return truncate(sanitize(args))

    keys = _CALL_KEYS.get(name)
    if keys is None:
        for prefix, pkeys in _PREFIX_KEYS:
            if name.startswith(prefix):
                keys = pkeys
                break
    if keys:
        for k in keys:
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                return truncate(sanitize(v) + _scope_suffix(name, args))

    return truncate(compact_summary(args))


def compact_summary(args: Any) -> str:
    """Generic fallback summary (port of ChatRenderer._compact_summary)."""
    if not isinstance(args, dict):
        s = sanitize(args)
        return truncate(s, 80)
    reason = args.get("reason")
    if isinstance(reason, str) and reason.strip():
        return truncate(sanitize(reason), 80)
    for key in ("path", "file_path", "target", "directory"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return sanitize(v)
    paths = args.get("paths")
    if isinstance(paths, list) and paths:
        head = ", ".join(sanitize(p) for p in paths[:2])
        return head + ("…" if len(paths) > 2 else "")
    for key in ("query", "pattern", "name", "command", "question"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return f'{key}="{truncate(sanitize(v), 80)}"'
    items: list[str] = []
    for k, v in args.items():
        items.append(f"{k}={truncate(sanitize(v), 40)}")
    return truncate(", ".join(items), 80)


# ---------------------------------------------------------------------------
# Result summaries
# ---------------------------------------------------------------------------


def _envelope(body: str) -> tuple[Any, Any]:
    """Parse the ``{"status": ..., "result": ...}`` tr_* envelope.

    Returns ``(parsed_json_or_None, result_payload_or_None)``.
    """
    stripped = body.strip() if isinstance(body, str) else ""
    if not stripped.startswith(("{", "[")):
        return None, None
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None, None
    if isinstance(obj, dict):
        return obj, obj.get("result")
    return obj, None


def format_result(name: str, body: str, is_err: bool) -> str:
    """Short outcome line for a tool result ("3 matches", "exit 0", error)."""
    if is_err:
        return truncate(error_message(body) or "error", MAX_RESULT_LEN)

    obj, result = _envelope(body)

    if isinstance(result, dict):
        # Shell-shaped results (tr_execute*, tr_grep, tr_run_python …).
        if "exit_code" in result:
            stdout = str(result.get("stdout") or "")
            lines = [ln for ln in stdout.splitlines() if ln.strip()]
            if name == "tr_grep":
                n = len(lines)
                return f"{n} matching line{'s' if n != 1 else ''}"
            code = result.get("exit_code")
            if lines:
                first = truncate(sanitize(lines[0]), 70)
                more = f" (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
                return f"{first}{more}"
            return f"exit {code}" if code not in (0, None) else "ok"
        # File reads.
        content = result.get("content")
        if isinstance(content, str):
            n = content.count("\n") + 1 if content else 0
            return f"{n} line{'s' if n != 1 else ''}"
        # Directory/glob listings.
        entries = result.get("entries")
        if isinstance(entries, list):
            n = len(entries)
            return f"{n} entr{'ies' if n != 1 else 'y'}"
        # Subagent-ish nested response.
        response = result.get("response")
        if isinstance(response, str) and response.strip():
            return truncate(sanitize(response), MAX_RESULT_LEN)

    if isinstance(obj, list):
        return f"{len(obj)} item{'s' if len(obj) != 1 else ''}"
    if isinstance(obj, dict) and not result:
        # Bare status dicts / loose payloads.
        msg = obj.get("message") or obj.get("detail")
        if isinstance(msg, str) and msg.strip():
            return truncate(sanitize(msg), MAX_RESULT_LEN)
        if str(obj.get("status", "")).lower() in ("success", "ok"):
            return "ok"

    first = ""
    if isinstance(body, str):
        for ln in body.splitlines():
            if ln.strip():
                first = ln.strip()
                break
    return truncate(sanitize(first), MAX_RESULT_LEN) if first else "ok"


# ---------------------------------------------------------------------------
# Shared error / pretty helpers (moved from ChatRenderer statics)
# ---------------------------------------------------------------------------


def looks_like_error(body: str) -> bool:
    if not isinstance(body, str) or not body:
        return False
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                status = str(obj.get("status", "")).lower()
                if status in ("error", "rejected", "fail", "failed"):
                    return True
                if obj.get("error"):
                    return True
        except (json.JSONDecodeError, ValueError):
            pass
    head = stripped[:200].lower()
    markers = ("error:", "exception:", "traceback", "failed:", "✗")
    return any(m in head for m in markers)


def error_message(body: str) -> str:
    if not isinstance(body, str):
        return ""
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                err = obj.get("error")
                if isinstance(err, dict):
                    msg = err.get("message") or err.get("code")
                    if msg:
                        return str(msg)[:120]
                elif isinstance(err, str) and err:
                    return err[:120]
                msg = obj.get("message")
                if isinstance(msg, str) and msg:
                    return msg[:120]
        except (json.JSONDecodeError, ValueError):
            pass
    first = stripped.splitlines()[0] if stripped else ""
    return first[:120]


def pretty_body(body: str) -> str:
    if not isinstance(body, str):
        return str(body)
    stripped = body.strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.dumps(json.loads(stripped), indent=2, default=str)
        except (json.JSONDecodeError, ValueError):
            pass
    return body


def extract_subagent_response(body: str) -> str:
    """The subagent's final message from a ``task`` tool result.

    Falls back to the raw body when the envelope shape isn't recognized.
    """
    obj, result = _envelope(body)
    if isinstance(result, dict):
        for key in ("response", "content", "output", "text"):
            v = result.get(key)
            if isinstance(v, str) and v.strip():
                return v
    if isinstance(obj, dict):
        for key in ("response", "content", "output", "text"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v
    return body if isinstance(body, str) else str(body)
