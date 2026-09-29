"""The marks the transcript is drawn with, named once.

Scattered, these drifted: the assistant's prose opened with ``🤖`` in both
renderers, todos used ``☐``/``◐``/``✓`` in one and ``[ ]``/``[▶]``/``[✓]`` in
the other, and the plain renderer marked a tool call with ``·`` where the rich
one used ``●``. A reader cannot learn a vocabulary that changes with the
terminal it is read in.

Every mark here is one cell wide in every font. That rules out emoji: ``🤖``
is two cells in some terminals, one in others and a replacement box in a few,
so the column it sat in moved depending on who was looking.
"""

from __future__ import annotations

#: Speakers.
YOU = "❯"  # your message
AGENT = "◇"  # the assistant's prose
TOOL = "●"  # a tool call
SUBAGENT = "◆"  # a delegated call
RESULT = "⎿"  # the result hanging off a call

#: Outcomes.
OK = "✓"
FAIL = "✗"

#: Plan state. Done is grey, running is bold, pending is plain — the weight
#: carries the state, so these three need no colour of their own.
PLAN = "▸"
TODO_DONE = "✓"
TODO_RUNNING = "▶"
TODO_PENDING = "·"

#: Asides.
ARTIFACT = "◨"
NOTE = "·"

#: The prompt. Two cells including its trailing space.
PROMPT = "❯ "

__all__ = [
    "AGENT",
    "ARTIFACT",
    "FAIL",
    "NOTE",
    "OK",
    "PLAN",
    "PROMPT",
    "RESULT",
    "SUBAGENT",
    "TODO_DONE",
    "TODO_PENDING",
    "TODO_RUNNING",
    "TOOL",
    "YOU",
]
