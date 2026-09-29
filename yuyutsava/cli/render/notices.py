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

#: The transcript's ladder: a marker naming the speaker, then what they said.
#: Restated here rather than imported because this module is below the
#: renderers that own the ladder, and it may not import one.
_MARKER_INDENT = 2
_BODY_INDENT = 4


def warning_rows(message: Any, category: type, filename: str, lineno: int) -> list[str]:
    """A Python warning as two transcript lines: what warned, then what it said.

    ``warnings.formatwarning`` produces an absolute path, the category and the
    message, then an echo of the offending source line. In a transcript pane
    that is a wall of ``site-packages`` interrupting the conversation to say
    something about a library the reader did not call.

    Two lines rather than one long one, because a warning is usually longer
    than the pane and the continuation has to land somewhere. On one line it
    lands under the label, where it reads as a second warning; given a line of
    its own at the body indent, the message wraps under itself and the whole
    thing sits on the same ladder as every other block — a marker naming the
    speaker at column two, what they said at column four.

    Here rather than in a renderer because the stdio capture needs it and may
    not import one: the capture is what a warning reaches, and it has no idea
    which renderer is running.
    """
    module = Path(filename).stem or "warning"
    text = _FLAT.sub(" ", str(message)).strip()
    return [
        f"{' ' * _MARKER_INDENT}{WARN}{notice_mark('warning')} {category.__name__}:{RESET}"
        f"  {CHROME}{module}:{lineno}{RESET}",
        f"{' ' * _BODY_INDENT}{text}",
    ]


def action_hint(data: dict[str, Any]) -> str:
    """``"→ /retry · /new"`` for a notice's actions; ``""`` when it has none."""
    commands = [
        ACTION_COMMANDS[a["id"]]
        for a in data.get("actions") or ()
        if isinstance(a, dict) and a.get("id") in ACTION_COMMANDS
    ]
    return "→ " + " · ".join(commands) if commands else ""
