"""Wrap ANSI-coloured text to a column width, without breaking the colours.

The chat dashboard's left pane must never draw past its own edge: one long
line and the transcript is painted straight across the context column beside
it. Rich wraps its own output to the pane width, but it is not the only writer
— ``warnings.warn`` and every ``logging`` record arrive as one unbounded line,
and a ``print`` can be any length at all. So the *view* wraps, at render time,
and the buffer stores logical lines. That also means a terminal that gets
narrower re-wraps correctly instead of bleeding.

Two things make this more than ``textwrap``:

* **escape sequences have no width.** Measuring ``len()`` on coloured text
  overestimates wildly and wraps far too early.
* **colour spans the break.** A line split in the middle of a coloured run has
  to re-open that run on the continuation, or the rest of the line loses its
  colour — and the pane must be reset at each break so nothing leaks into the
  next row.

East-Asian wide characters count as two columns and combining marks as zero,
because a pane is measured in cells, not codepoints.
"""

from __future__ import annotations

import re
import unicodedata

#: CSI / OSC escape sequences. Only SGR (``…m``) affects later text, but every
#: sequence is zero-width and must be carried through the wrap untouched.
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;:?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")

#: Turns every attribute off. Emitted at a wrap break so a colour cannot run
#: into the pane's neighbour.
_RESET = "\x1b[0m"

_SGR_RESET = ("\x1b[0m", "\x1b[m")


def char_width(ch: str) -> int:
    """Display cells for one character: 0 for combining, 2 for wide, else 1."""
    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_width(text: str) -> int:
    """Display width of *text*, ignoring escape sequences."""
    return sum(char_width(c) for c in _ANSI_RE.sub("", text))


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _tokens(line: str) -> list[tuple[bool, str]]:
    """Split into ``(is_escape, text)`` runs, in order."""
    out: list[tuple[bool, str]] = []
    pos = 0
    for m in _ANSI_RE.finditer(line):
        if m.start() > pos:
            out.append((False, line[pos:m.start()]))
        out.append((True, m.group()))
        pos = m.end()
    if pos < len(line):
        out.append((False, line[pos:]))
    return out


def wrap_ansi(line: str, width: int) -> list[str]:
    """Wrap one logical line to *width* cells, preserving colour across breaks.

    Returns at least one element (``[""]`` for an empty line) so a blank line
    in the transcript stays a blank row. Hard-wraps: a pane is not the place to
    keep a 4,000-character base64 blob on one row, and word-wrapping a log line
    full of paths reads worse than a clean break.

    A character wider than the entire pane becomes ``…`` — one cell, and
    visibly an elision. Emitting it whole would overflow by a cell, and the
    no-overflow guarantee has to hold at every width or it is not a guarantee.
    """
    if width <= 0:
        return [line]
    if visible_width(line) <= width:
        return [line]

    lines: list[str] = []
    active: list[str] = []      # SGR sequences still in effect
    current: list[str] = []     # pieces of the row being built
    col = 0

    def flush() -> None:
        nonlocal col
        text = "".join(current)
        lines.append(text + _RESET if active else text)
        current.clear()
        # Re-open the colours that were in effect at the break.
        if active:
            current.extend(active)
        col = 0

    for is_escape, chunk in _tokens(line):
        if is_escape:
            current.append(chunk)
            if chunk in _SGR_RESET:
                active.clear()
            elif chunk.endswith("m"):
                active.append(chunk)
            continue
        for ch in chunk:
            w = char_width(ch)
            if w > width:
                # A glyph wider than the whole pane (a 2-cell character in a
                # 1-cell column). Emitting it would overflow by a cell, which
                # is the bleed this module exists to prevent, so substitute an
                # ellipsis: one cell, and visibly an elision rather than a
                # silently dropped character.
                if col + 1 > width and col > 0:
                    flush()
                current.append("…")
                col += 1
                continue
            if col + w > width and col > 0:
                flush()
            current.append(ch)
            col += w

    tail = "".join(current)
    # A trailing row that is only re-opened escapes carries no text; drop it.
    if strip_ansi(tail):
        lines.append(tail + _RESET if active else tail)
    elif not lines:
        lines.append(tail)
    return lines


def wrap_lines(lines: list[str], width: int) -> list[str]:
    """``wrap_ansi`` over a list, flattened."""
    out: list[str] = []
    for line in lines:
        out.extend(wrap_ansi(line, width))
    return out


__all__ = [
    "char_width",
    "strip_ansi",
    "visible_width",
    "wrap_ansi",
    "wrap_lines",
]
