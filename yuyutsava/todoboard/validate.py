"""The board's contract on incoming values.

Separate from the services because it is the one part with no state and no
store: a check is a function of its argument, which is what lets every service
share it without reaching for a sibling. The messages are part of the contract
— the REST layer returns them verbatim in a 400 and the ``todo_*`` tools hand
them back to the agent — so they are the thing to keep stable, not the
predicates.
"""

from __future__ import annotations

from yuyutsava.todoboard.errors import require
from yuyutsava.todoboard.models import (
    ATTACHMENT_KINDS,
    CARD_STATUSES,
    MAX_NOTE_LEN,
    MAX_REASON_LEN,
    MAX_TITLE_LEN,
    NOTE_AUTHORS,
    OBJECTIVE_PHASES,
)


#: Cards and objectives share a title limit but not a noun; the caller passes
#: the noun so the message still names what the user actually sent.
def title(text: str, what: str) -> str:
    text = (text or "").strip()
    require(bool(text), f"{what} title must be non-empty")
    require(len(text) <= MAX_TITLE_LEN, f"{what} title exceeds {MAX_TITLE_LEN} chars")
    return text


def body(text: str) -> str:
    text = (text or "").strip()
    require(bool(text), "note body must be non-empty")
    require(len(text) <= MAX_NOTE_LEN, f"note body exceeds {MAX_NOTE_LEN} chars")
    return text


def status(value: str) -> None:
    require(value in CARD_STATUSES, f"status must be one of {CARD_STATUSES}")


def phase(value: str) -> None:
    require(value in OBJECTIVE_PHASES, f"phase must be one of {OBJECTIVE_PHASES}")


def author(value: str) -> None:
    require(value in NOTE_AUTHORS, f"author must be one of {NOTE_AUTHORS}")


def kind(value: str) -> None:
    require(value in ATTACHMENT_KINDS, f"kind must be one of {ATTACHMENT_KINDS}")


def reason(text: str | None) -> str | None:
    """Reasons and outcomes are prose, and an empty one is no reason at all."""
    if text is None:
        return None
    text = text.strip()
    require(len(text) <= MAX_REASON_LEN, f"text exceeds {MAX_REASON_LEN} chars")
    return text or None


def tags(values: list[str] | None) -> list[str]:
    values = [str(t).strip() for t in (values or []) if str(t).strip()]
    require(len(values) <= 50, "at most 50 tags per card")
    return values


def limit(value: int) -> None:
    """Guards the list endpoints: a board is hundreds of cards, so a caller
    asking for more is a bug, not a big board."""
    require(1 <= value <= 5_000, "limit must be between 1 and 5000")


__all__ = [
    "author",
    "body",
    "kind",
    "limit",
    "phase",
    "reason",
    "status",
    "tags",
    "title",
]
