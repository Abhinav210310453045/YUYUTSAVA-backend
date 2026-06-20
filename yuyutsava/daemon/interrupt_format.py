"""Human-readable formatting for LangGraph ``interrupt()`` values.

A single place that turns a raw interrupt value (task-runner permission,
subagent question, raw-execute permission, …) into a clean ``title`` / ``body`` /
``options`` triple. Used by BOTH the orchestrator's ask handler
(:func:`yuyutsava.daemon.orchestrator_loop.make_ask_handler`) and the background
``AsyncTaskHealthWatcher`` so foreground and background asks render identically —
no raw-JSON blobs leaking into the CLI prompt or the UI ``AskCard``.

Kept dependency-free (stdlib only) so the watcher can import it without a cycle.
"""

from __future__ import annotations

import json


def title_for_interrupt(iv: dict) -> str:
    if not isinstance(iv, dict):
        return "Permission request"
    t = iv.get("type", "")
    if t == "task_runner_permission":
        op = (iv.get("operation") or "").upper()
        return f"Permission: {op}"
    if t == "user_question":
        return "Subagent question"
    return iv.get("title") or "Permission request"


def body_for_interrupt(iv: dict) -> str:
    if not isinstance(iv, dict):
        return str(iv)
    t = iv.get("type", "")
    if t == "task_runner_permission":
        paths = iv.get("paths", [])
        op = str(iv.get("operation", "?")).upper()
        reason = iv.get("reason", "")
        risk = iv.get("risk_level", "")
        zone = iv.get("zone", "")
        path_str = ", ".join(paths) if isinstance(paths, list) else str(paths)
        lines = [f"{op}  {path_str}"]
        if reason:
            lines += ["", str(reason)]
        meta = " · ".join(
            x for x in (f"zone: {zone}" if zone else "", f"risk: {risk}" if risk else "") if x
        )
        if meta:
            lines += ["", meta]
        return "\n".join(lines)
    if t == "user_question":
        return iv.get("question", "")
    # PermissionMiddleware (raw execute) — show both command and reason so
    # the Electron card carries the same "what / why" that the CLI prompts
    # already include.
    if t == "permission_request":
        command = iv.get("command", "")
        reason = iv.get("reason", "")
        if command and reason:
            return f"{command}\n\n{reason}"
        return command or reason or json.dumps(iv)[:300]
    return iv.get("command") or iv.get("reason") or json.dumps(iv)[:300]


def options_for_interrupt(iv: dict) -> list[str]:
    if not isinstance(iv, dict):
        return ["approve", "reject"]
    t = iv.get("type", "")
    if t == "user_question":
        return list(iv.get("options") or [])
    if t == "task_runner_permission":
        # Claude/Cursor-style scope choices: approve once vs. allow for the whole
        # session / project. "approve" stays the once option (back-compat). Every
        # operation type (read/write/delete/execute/…) is allowlistable so a
        # remembered decision spans the op for the session/project — matching
        # Claude Code's per-tool permission rules.
        return ["approve", "session", "project", "reject"]
    return ["approve", "reject"]
