"""How the terminal shows a notice: its mark, and the commands for its actions.

A notice names what the user can do about it as actions
(``conversation.recovery.NOTICE_ACTIONS``), not as a command — the app offers
the same actions as buttons. In the chat REPL each action is a slash command,
so the renderers append ``→ /retry · /new``. Both terminal renderers read this
one table; ``test/cli/test_notice_actions.py`` pins every entry to
``SLASH_COMMANDS`` and every action to an entry.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from yuyutsava.ansi import CHROME, RESET, WARN

#: The REPL command that does each notice action.
ACTION_COMMANDS: dict[str, str] = {"retry": "/retry", "new_chat": "/new"}

_MARKS = {"error": "✗", "warning": "⚠"}


def notice_mark(level: str) -> str:
    """``✗`` for an error, ``⚠`` for a warning, ``·`` for anything else."""
    return _MARKS.get(level, "·")


#: Collapses the newlines and runs of spaces a warning message may carry.
_FLAT = re.compile(r"\s+")


def warning_line(message: Any, category: type, filename: str, lineno: int) -> str:
    """One themed row for a Python warning, mark and colour included.

    ``warnings.formatwarning`` produces two lines: an absolute path, the
    category and the message, then an echo of the offending source line. In a
    transcript pane that is a wall of ``site-packages`` at column zero,
    interrupting the conversation to say something about a library the reader
    did not call. The same information fits on one row — what warned, and what
    it said — indented like every other aside the renderer draws.

    Here rather than in a renderer because the stdio capture needs it and may
    not import one: the capture is what a warning reaches, and it has no idea
    which renderer is running.
    """
    module = Path(filename).stem or "warning"
    text = _FLAT.sub(" ", str(message)).strip()
    return (
        f"  {WARN}{notice_mark('warning')} {category.__name__}{RESET}"
        f"  {CHROME}{module}:{lineno}{RESET}  {text}"
    )


def action_hint(data: dict[str, Any]) -> str:
    """``"→ /retry · /new"`` for a notice's actions; ``""`` when it has none."""
    commands = [
        ACTION_COMMANDS[a["id"]]
        for a in data.get("actions") or ()
        if isinstance(a, dict) and a.get("id") in ACTION_COMMANDS
    ]
    return "→ " + " · ".join(commands) if commands else ""
