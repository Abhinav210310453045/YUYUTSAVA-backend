"""The moving mark that says a turn is still alive.

A turn spends most of its time waiting: on the model's first token, on a tool,
on a subagent. A static ``Thinking…`` cannot tell that apart from a wedged
process, and the wait is exactly when a reader starts wondering whether to
press Ctrl+C. A mark that moves answers the question without being read.

The arithmetic is here, free of Rich and of prompt_toolkit, because both draw
it: the scrolling transcript has a ``Live`` region and the split view has a
status bar, and a second implementation of the phase maths is a second thing
to get out of step. Each consumer turns a frame and a role into its own kind
of styled text.

Time is passed in rather than read, so a test can ask what the mark looks like
at a given instant instead of sleeping.
"""

from __future__ import annotations

#: Braille cells, which are one column wide in every font that has them and
#: degrade to a box rather than to a double-width glyph in the few that do not.
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: Roles the mark walks through, from the palette the transcript already uses.
#: Colour here carries no meaning — nothing about the wait is blue and then
#: green — it is motion by another means, which is why the cycle is longer
#: than the frame rate and the two are deliberately out of phase.
HUES = ("you", "tool", "ok", "warn", "subagent")

#: Seconds a single frame is held. Matched to the split view's repaint
#: interval: asking for frames faster than the screen is redrawn buys nothing
#: and skips some of them unevenly.
FRAME_SEC = 0.12

#: Seconds a single colour is held.
HUE_SEC = 0.6


def frame_at(now: float) -> str:
    """The braille cell showing at *now* (a monotonic reading, in seconds)."""
    return FRAMES[int(now / FRAME_SEC) % len(FRAMES)]


def hue_at(now: float) -> str:
    """The palette role the mark is drawn in at *now*."""
    return HUES[int(now / HUE_SEC) % len(HUES)]


def is_animated(state: str) -> bool:
    """Whether *state* is a wait, rather than output already arriving.

    ``writing`` means tokens are landing on screen, which says the turn is
    alive far better than a spinner does; the mark settles then and the prose
    carries it.
    """
    return state in ("thinking", "tool", "subagent", "retry")


#: What a settled mark looks like: the same width as a frame, so the label
#: beside it does not step sideways when the animation stops.
RESTING = "⠿"


def mark_at(now: float, state: str) -> str:
    """The cell to draw for *state* at *now*."""
    return frame_at(now) if is_animated(state) else RESTING


__all__ = [
    "FRAMES",
    "FRAME_SEC",
    "HUES",
    "HUE_SEC",
    "RESTING",
    "frame_at",
    "hue_at",
    "is_animated",
    "mark_at",
]
