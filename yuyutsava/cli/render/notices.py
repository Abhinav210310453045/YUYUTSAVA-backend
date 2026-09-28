"""How the terminal shows a notice: its mark, and the commands for its actions.

A notice names what the user can do about it as actions
(``conversation.recovery.NOTICE_ACTIONS``), not as a command — the app offers
the same actions as buttons. In the chat REPL each action is a slash command,
so the renderers append ``→ /retry · /new``. Both terminal renderers read this
one table; ``test/cli/test_notice_actions.py`` pins every entry to
``SLASH_COMMANDS`` and every action to an entry.
"""

from __future__ import annotations

from typing import Any

#: The REPL command that does each notice action.
ACTION_COMMANDS: dict[str, str] = {"retry": "/retry", "new_chat": "/new"}

_MARKS = {"error": "✗", "warning": "⚠"}


def notice_mark(level: str) -> str:
    """``✗`` for an error, ``⚠`` for a warning, ``·`` for anything else."""
    return _MARKS.get(level, "·")


def action_hint(data: dict[str, Any]) -> str:
    """``"→ /retry · /new"`` for a notice's actions; ``""`` when it has none."""
    commands = [
        ACTION_COMMANDS[a["id"]]
        for a in data.get("actions") or ()
        if isinstance(a, dict) and a.get("id") in ACTION_COMMANDS
    ]
    return "→ " + " · ".join(commands) if commands else ""
