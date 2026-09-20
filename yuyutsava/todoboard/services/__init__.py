"""The board's write path, one domain per module.

:class:`~yuyutsava.todoboard.exchange.TodoExchange` is the facade over these
and stays the only thing callers touch; a service is imported here, by the
facade, and nowhere else.
"""

from __future__ import annotations

from yuyutsava.todoboard.services.attachments import AttachmentService
from yuyutsava.todoboard.services.cards import CardService
from yuyutsava.todoboard.services.core import BoardCore, workspace_root
from yuyutsava.todoboard.services.notes import NoteService
from yuyutsava.todoboard.services.objectives import ObjectiveService

__all__ = [
    "AttachmentService",
    "BoardCore",
    "CardService",
    "NoteService",
    "ObjectiveService",
    "workspace_root",
]
