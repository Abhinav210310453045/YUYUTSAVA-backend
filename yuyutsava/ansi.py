"""The terminal palette, in one place.

Nine escape codes were written out 157 times across ten modules, and only one of
them named any of them (`R-DRY-04`). A literal like ``"\\033[33m"`` is
unreviewable — nothing distinguishes a deliberate yellow from a typo'd one — and
changing the scheme meant finding every copy.

A leaf beside :mod:`yuyutsava.text`, not under ``cli/render``, because the
daemon's terminal channel writes these too: a palette under ``cli`` would make
``daemon`` import ``cli``.

Two layers live here. The **weights and the sixteen-colour names** are the raw
vocabulary, unchanged, and the daemon writes with them. The **roles** below are
the chat REPL's semantic palette: one hue per *kind* of thing, with state left
to weight and glyph. Before them, ``tool`` was ``bold cyan``, ``accent`` was
``cyan`` and ``prompt`` was ``bold cyan`` — three meanings sharing one colour,
which is why a transcript read as one undifferentiated block.
"""

from __future__ import annotations

import os

RESET = "\033[0m"

# Weight
DIM = "\033[2m"
BOLD = "\033[1m"

# Colour. These are the only ones in use; add a name here rather than a literal
# at the call site.
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"

#: Reserved for a failure the user must not scroll past.
BOLD_RED = "\033[1;31m"

# Movement. Not colour, but the same vocabulary and the same reason to name it:
# a bare "\033[2J\033[H" at a call site is unreviewable.
#: Erase the screen and put the cursor at the top-left — what `/clear` does.
CLEAR_SCREEN = "\033[2J\033[H"


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
#
# Each role is ``(hex, xterm-256 index)``. Both are needed: Rich and
# prompt_toolkit take the hex and degrade it themselves, while the raw escapes
# below have to pick one form up front, because a terminal that does not
# understand ``38;2`` prints the digits.
#
# Two ramps, because the CLI cannot set the terminal's background and nothing
# is legible on both black and white at one value. The values are GitHub's
# dark and light syntax palettes, which are already tested at these contrasts.

_DARK: dict[str, tuple[str, int]] = {
    "you": ("#58a6ff", 75),  # your message, and its marker
    "agent": ("#39d3de", 44),  # the assistant's own voice
    "tool": ("#39d3de", 44),  # a tool call: the machine acting
    "ok": ("#3fb950", 71),  # a result that came back
    "err": ("#ff6b61", 203),  # a result that failed
    "warn": ("#e3b341", 179),  # wants an answer: warnings, the external zone
    "subagent": ("#a371f7", 141),  # delegation
    "chrome": ("#6e7681", 244),  # ring indices, arguments, footers
    # The opening screen only. Its rows name facts about the session rather
    # than anything in the conversation, so they take two hues the transcript
    # never uses — rose is the one gap the seven above leave on the wheel, and
    # a warm neutral reads as data beside it. A banner drawn in transcript
    # colours invites reading a session id as if it were a tool result.
    "key": ("#ff8fab", 211),  # a banner row's label
    "value": ("#d5c4a1", 180),  # a banner row's value
}

_LIGHT: dict[str, tuple[str, int]] = {
    "you": ("#0a58ca", 26),
    "agent": ("#0e7c86", 30),
    "tool": ("#0e7c86", 30),
    "ok": ("#1a7f37", 28),
    "err": ("#cf222e", 160),
    "warn": ("#9a6700", 136),
    "subagent": ("#6639ba", 92),
    "chrome": ("#57606a", 242),
    "key": ("#a4133c", 125),
    "value": ("#6f5c3e", 94),
}

ROLES = tuple(_DARK)


def resolve_theme(env: dict[str, str] | None = None) -> str:
    """``"dark"`` or ``"light"``, from the environment.

    ``YUYUTSAVA_THEME`` wins. Otherwise ``COLORFGBG`` — which terminals that
    set it end with the background colour index, low numbers being dark — and
    failing that, dark, because that is what most terminals running this are.
    """
    env = os.environ if env is None else env
    choice = (env.get("YUYUTSAVA_THEME") or "").strip().lower()
    if choice in ("dark", "light"):
        return choice
    fgbg = (env.get("COLORFGBG") or "").strip()
    if ";" in fgbg:
        tail = fgbg.rsplit(";", 1)[-1]
        if tail.isdigit():
            return "light" if int(tail) >= 7 else "dark"
    return "dark"


def palette(theme: str | None = None) -> dict[str, str]:
    """Role → hex, for the consumers that take hex (Rich, prompt_toolkit)."""
    ramp = _LIGHT if (theme or resolve_theme()) == "light" else _DARK
    return {role: value[0] for role, value in ramp.items()}


def truecolor(env: dict[str, str] | None = None) -> bool:
    """Whether to emit 24-bit escapes rather than 256-colour ones.

    Terminals that support 24-bit colour advertise it in ``COLORTERM``; the
    ones that do not (Apple's Terminal, most of what runs over ssh) print the
    parameters of a ``38;2`` sequence as text, so the 256-colour index is the
    safe default rather than the degraded one.
    """
    env = os.environ if env is None else env
    return (env.get("COLORTERM") or "").strip().lower() in ("truecolor", "24bit")


def sgr(role: str, *, theme: str | None = None, full: bool | None = None) -> str:
    """The escape sequence that turns *role*'s colour on."""
    ramp = _LIGHT if (theme or resolve_theme()) == "light" else _DARK
    hex_value, index = ramp[role]
    if full if full is not None else truecolor():
        r, g, b = (int(hex_value[i : i + 2], 16) for i in (1, 3, 5))
        return f"\033[38;2;{r};{g};{b}m"
    return f"\033[38;5;{index}m"


#: The roles as ready-to-print escapes. Resolved once, at import: a process
#: draws in one theme for its whole life, and every call site uses these the
#: way it used ``CYAN`` — as a string it can concatenate.
YOU = sgr("you")
AGENT = sgr("agent")
TOOL = sgr("tool")
OK = sgr("ok")
ERR = sgr("err")
WARN = sgr("warn")
SUBAGENT = sgr("subagent")
CHROME = sgr("chrome")
KEY = sgr("key")
VALUE = sgr("value")

__all__ = [
    "AGENT",
    "BOLD",
    "CLEAR_SCREEN",
    "BOLD_RED",
    "CHROME",
    "CYAN",
    "DIM",
    "ERR",
    "GREEN",
    "KEY",
    "MAGENTA",
    "OK",
    "RED",
    "RESET",
    "ROLES",
    "SUBAGENT",
    "TOOL",
    "VALUE",
    "WARN",
    "YELLOW",
    "YOU",
    "palette",
    "resolve_theme",
    "sgr",
    "truecolor",
]
