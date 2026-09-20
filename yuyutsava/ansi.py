"""The terminal palette, in one place.

Nine escape codes were written out 157 times across ten modules, and only one of
them named any of them (`R-DRY-04`). A literal like ``"\\033[33m"`` is
unreviewable — nothing distinguishes a deliberate yellow from a typo'd one — and
changing the scheme meant finding every copy.

A leaf beside :mod:`yuyutsava.text`, not under ``cli/render``, because the
daemon's terminal channel writes these too: a palette under ``cli`` would make
``daemon`` import ``cli``.
"""

from __future__ import annotations

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

__all__ = [
    "BOLD",
    "CLEAR_SCREEN",
    "BOLD_RED",
    "CYAN",
    "DIM",
    "GREEN",
    "MAGENTA",
    "RED",
    "RESET",
    "YELLOW",
]
