"""Shared rich Console for the chat REPL.

One console, one stream: rich mode sends the whole transcript (chrome and
prose) to stdout because a ``rich.live.Live`` region cannot interleave
safely across two output streams. The plain fallback renderer keeps the
historical stdout/stderr split.
"""

from __future__ import annotations

import os
import sys

from rich.console import Console
from rich.theme import Theme

# Mirrors the ANSI palette in chat_repl.py so both renderers look related.
_THEME = Theme(
    {
        "accent": "cyan",
        "chrome": "dim",
        "ok": "green",
        "err": "red",
        "warn": "yellow",
        "tool": "bold cyan",
        "subagent": "bold magenta",
    }
)


def rich_capable() -> bool:
    """Whether the rich transcript renderer should be used.

    Requires a real TTY on stdout and a terminal that isn't ``dumb``;
    ``YUYUTSAVA_REPL_RICH=0`` opts out (same flag style as
    ``YUYUTSAVA_REPL_SMOOTH``).
    """
    flag = os.environ.get("YUYUTSAVA_REPL_RICH", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if os.environ.get("TERM", "").strip().lower() == "dumb":
        return False
    try:
        return bool(sys.stdout.isatty())
    except (ValueError, AttributeError):
        return False


def make_console() -> Console:
    return Console(file=sys.stdout, theme=_THEME, highlight=False)
