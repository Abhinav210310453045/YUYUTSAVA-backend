"""Humanized permission / question cards for the chat REPL.

Turns the typed interrupt payloads (``task_runner_permission``,
``permission_request``, ``user_question`` — see
``yuyutsava/models/interrupts.py``) into a plain-English headline the
user can act on without decoding a key/value dump, e.g.::

    ▣ The agent wants to modify yuyutsava/foo.py
      workspace zone · medium risk · requested by task_runner
      "reformat before commit"
      [y] allow once   [s] allow this session   [p] always in this project   [n] deny

The sentence builders are renderer-independent (plain strings) so the
ANSI fallback shares them; the rich ``Panel`` variant lives here too.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from yuyutsava.cli.render.tool_format import sanitize, truncate

# operation → human verb ("The agent wants to <verb> <paths>").
_OP_VERBS = {
    "read": "read",
    "write": "modify",
    "create": "create",
    "delete": "delete",
    "move": "move",
    "copy": "copy",
    "chmod": "change permissions on",
    "execute": "run a command in",
    "elevate": "elevate privileges for",
}

_TR_OPTIONS = "[y] allow once   [s] allow this session   [p] always in this project   [n] deny"
_EXEC_OPTIONS = "[y] allow once   [n] deny"


def _paths_phrase(paths: Any) -> str:
    if isinstance(paths, list) and paths:
        shown = [sanitize(p) for p in paths[:3]]
        extra = f" (+{len(paths) - 3} more)" if len(paths) > 3 else ""
        return ", ".join(shown) + extra
    if isinstance(paths, str) and paths.strip():
        return sanitize(paths)
    return ""


def headline(payload: dict[str, Any]) -> str:
    """One plain-English sentence saying what is being asked."""
    itype = payload.get("type", "")
    if itype == "task_runner_permission":
        op = str(payload.get("operation") or "").lower()
        verb = _OP_VERBS.get(op, op or "act on")
        target = _paths_phrase(payload.get("paths")) or "your files"
        return f"The agent wants to {verb} {target}"
    if itype == "permission_request":
        return "The agent wants to run a command"
    if itype == "user_question":
        return sanitize(payload.get("title") or "The agent has a question")
    return sanitize(payload.get("title") or "Permission requested")


def subtitle(payload: dict[str, Any]) -> str:
    """Dim context line: zone · risk · requesting agent."""
    bits: list[str] = []
    zone = payload.get("zone")
    if zone:
        bits.append(f"{sanitize(zone).lower()} zone")
    risk = payload.get("risk_level")
    if risk:
        bits.append(f"{sanitize(risk).lower()} risk")
    agent = payload.get("requesting_agent")
    if agent:
        who = f"requested by {sanitize(agent)}"
        parent = payload.get("parent_agent")
        if parent:
            who += f" (parent: {sanitize(parent)})"
        bits.append(who)
    return " · ".join(bits)


def options_hint(itype: str) -> str:
    return _TR_OPTIONS if itype == "task_runner_permission" else _EXEC_OPTIONS


def print_ask_panel(console: Console, payload: dict[str, Any]) -> None:
    """Rich card for a permission/question interrupt."""
    itype = payload.get("type", "")

    if itype == "user_question":
        body = payload.get("body") or payload.get("question") or ""
        parts: list[Any] = []
        if body:
            parts.append(Markdown(str(body)))
        console.print(
            Panel(
                Group(*parts) if parts else Text(""),
                title=f"? {headline(payload)}",
                title_align="left",
                border_style="accent",
                padding=(0, 2),
            )
        )
        return

    parts = []
    sub = subtitle(payload)
    if sub:
        parts.append(Text(sub, style="chrome"))
    command = payload.get("command")
    if command:
        parts.append(Syntax(str(command), "bash", word_wrap=True, background_color="default"))
    reason = payload.get("reason")
    if reason:
        parts.append(Text(f'"{truncate(sanitize(reason), 200)}"', style="default"))
    parts.append(Text(options_hint(itype), style="warn"))
    console.print(
        Panel(
            Group(*parts),
            title=f"▣ {headline(payload)}",
            title_align="left",
            border_style="warn",
            padding=(0, 2),
        )
    )
