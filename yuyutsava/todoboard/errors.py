"""The board's failure vocabulary, and the guard that raises it.

A leaf on purpose. These types are what every caller maps outcomes from — the
REST layer to HTTP status codes, the ``todo_*`` tools to structured error
strings — so the artifact blocks that raise them need them too. While they
lived in :mod:`yuyutsava.todoboard.exchange` alongside the write path, four
sibling modules imported the facade to reach them, and the facade had to defer
its imports of *them* to break the cycle it had created.
"""

from __future__ import annotations


class TodoError(Exception):
    """Base for all TODO-board failures."""


class TodoValidationError(TodoError):
    """Caller sent a value outside the exchange contract (HTTP 400)."""


class TodoNotFoundError(TodoError):
    """Unknown card/note/attachment id (HTTP 404)."""


class TodoStorageError(TodoError):
    """The backing store failed; the operation may be retried (HTTP 500)."""


class TodoAttachmentError(TodoError):
    """Blob/file handling failed — bad path, unwritable dir (HTTP 507)."""


def require(condition: bool, message: str) -> None:
    """Refuse a caller's value before the store ever sees it."""
    if not condition:
        raise TodoValidationError(message)


__all__ = [
    "TodoAttachmentError",
    "TodoError",
    "TodoNotFoundError",
    "TodoStorageError",
    "TodoValidationError",
    "require",
]
