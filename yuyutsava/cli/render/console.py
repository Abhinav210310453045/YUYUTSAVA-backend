"""Shared rich Console for the chat REPL.

One console, one stream: rich mode sends the whole transcript (chrome and
prose) to stdout because a ``rich.live.Live`` region cannot interleave
safely across two output streams. The plain fallback renderer keeps the
historical stdout/stderr split.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from rich.console import Console
from rich.theme import Theme

from yuyutsava.ansi import palette, truecolor
from yuyutsava.config.env import env_flag

# Derived from the role palette in yuyutsava/ansi.py rather than restated, so
# the Rich transcript, the plain renderer's escapes and the dashboard's
# prompt_toolkit styles cannot drift. They were three hand-kept lists, and
# they already disagreed: "tool" was bold cyan here and plain cyan there.
_P = palette()
#: Each role, plus a bold variant of it. The variant has to be a style of its
#: own: Rich resolves a *theme name* only when the whole string is that name,
#: so ``style="tool bold"`` parses as two colour words, finds neither, and
#: silently draws in no style at all — which is how the marker naming the
#: speaker came out the same colour as the prose under it.
_ROLE_STYLES = {role: value for role, value in _P.items()}
_ROLE_STYLES |= {f"{role}.bold": f"bold {value}" for role, value in _P.items()}
THEME = Theme(
    {
        **_ROLE_STYLES,
        # The name the panels and the artifact box already draw with.
        "accent": _P["agent"],
        # Rich's markdown defaults are a separate palette again, and they
        # spend the transcript's hues on things that are not kinds: inline
        # code arrived as bold cyan on a hard black ground, the same colour as
        # a tool name and on a background that fights whatever the terminal's
        # actually is. Prose and headings keep the terminal's own foreground —
        # hierarchy is weight here, and the hues stay for the markers.
        "markdown.code": "bold",
        "markdown.code_block": "",
        "markdown.h1": "bold",
        "markdown.h2": "bold",
        "markdown.h3": "bold",
        "markdown.h4": "bold",
        "markdown.item.bullet": _P["chrome"],
        "markdown.block_quote": _P["chrome"],
        "markdown.link": _P["you"],
        "markdown.link_url": _P["you"],
    }
)


def color_system() -> str:
    """``"truecolor"`` or ``"256"`` — never left to Rich to detect.

    For the split view only, where Rich writes into a
    :class:`TranscriptBuffer` rather than a tty: its own detection settles on
    ``standard`` there and flattens every role to the nearest of sixteen
    colours, so ``you``, ``tool`` and ``subagent`` all came out one bright
    cyan — the collision the roles exist to remove. The destination genuinely
    is a terminal, one pane over, so the decision is made here from
    ``COLORTERM``, the same signal Rich would have used had it been able to
    look. Everywhere else Rich still detects for itself.
    """
    return "truecolor" if truecolor() else "256"


def rich_capable() -> bool:
    """Whether the rich transcript renderer should be used.

    Requires a real TTY on stdout and a terminal that isn't ``dumb``;
    ``YUYUTSAVA_REPL_RICH=0`` opts out (same flag style as
    ``YUYUTSAVA_REPL_SMOOTH``).
    """
    if not env_flag("YUYUTSAVA_REPL_RICH", default=True, false=("0", "false", "no", "off")):
        return False
    if os.environ.get("TERM", "").strip().lower() == "dumb":
        return False
    try:
        return bool(sys.stdout.isatty())
    except (ValueError, AttributeError):
        return False


def make_console(
    *,
    file: Any | None = None,
    width: int | None = None,
    force_terminal: bool | None = None,
) -> Console:
    """The shared themed console.

    All three arguments exist for the chat dashboard, which points Rich at a
    :class:`~yuyutsava.cli.render.dashboard.TranscriptBuffer` sized to the left
    pane. ``force_terminal`` is required there: Rich drops colour when the
    destination is not a tty, and the destination genuinely is one — one pane
    over. Defaults reproduce the previous behaviour exactly.
    """
    return Console(
        file=file if file is not None else sys.stdout,
        theme=THEME,
        highlight=False,
        width=width,
        force_terminal=force_terminal,
        # Only where the caller has told us the destination is a terminal.
        # Rich emits styles whenever a colour system is set, terminal or not,
        # so forcing one unconditionally would write escape sequences into
        # piped output that has never had any.
        color_system=color_system() if force_terminal else "auto",
    )
