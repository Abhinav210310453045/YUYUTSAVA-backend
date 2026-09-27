"""An in-memory ``TodoStore``, so the exchange can be tested off the real board.

The board holds real work, so nothing here may reach it. This implements the
whole 20-method ABC in dictionaries and is deliberately dumb: it stores what it
is given and returns it, so a test that fails is a statement about
:class:`~yuyutsava.todoboard.exchange.TodoExchange` and not about SQLite.

``fail_on`` makes one method raise, which is how the rollback and
storage-fault-wrapping paths are reached — they are unreachable against a store
that always works.
"""

from __future__ import annotations

from typing import Any

from yuyutsava.todoboard.models import (
    TodoAttachmentV1,
    TodoCardSummaryV1,
    TodoCardV1,
    TodoEventV1,
    TodoNoteV1,
    TodoObjectiveV1,
)
from yuyutsava.todoboard.store import TodoStore


class FakeTodoStore(TodoStore):
    """Every row the board would hold, in dicts."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.cards: dict[str, TodoCardV1] = {}
        self.notes: dict[str, TodoNoteV1] = {}
        self.objectives: dict[str, TodoObjectiveV1] = {}
        self.attachments: dict[str, TodoAttachmentV1] = {}
        self.events: list[TodoEventV1] = []
        #: Name of a method that should raise instead of working.
        self.fail_on = fail_on

    def _maybe_fail(self, name: str) -> None:
        if self.fail_on == name:
            raise RuntimeError(f"fake store: {name} failed on purpose")

    # -- cards -------------------------------------------------------------

    async def add_card(self, card: TodoCardV1) -> None:
        self._maybe_fail("add_card")
        self.cards[card.card_id] = card

    async def get_card(self, card_id: str) -> TodoCardV1 | None:
        self._maybe_fail("get_card")
        card = self.cards.get(card_id)
        if card is None:
            return None
        # The real store hydrates children; mirror that so the exchange's
        # callers see the same shape.
        card.notes = [n for n in self.notes.values() if n.card_id == card_id]
        card.objectives = [o for o in self.objectives.values() if o.card_id == card_id]
        card.attachments = [a for a in self.attachments.values() if a.card_id == card_id]
        return card

    async def update_card(self, card_id: str, fields: dict[str, Any]) -> bool:
        self._maybe_fail("update_card")
        card = self.cards.get(card_id)
        if card is None:
            return False
        for key, value in fields.items():
            setattr(card, key, value)
        return True

    async def delete_card(self, card_id: str) -> bool:
        self._maybe_fail("delete_card")
        if card_id not in self.cards:
            return False
        del self.cards[card_id]
        # CASCADE, as the real schema does.
        for bucket in (self.notes, self.objectives, self.attachments):
            for key in [k for k, v in bucket.items() if v.card_id == card_id]:
                del bucket[key]
        self.events = [e for e in self.events if e.card_id != card_id]
        return True

    async def list_cards(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[TodoCardSummaryV1]:
        self._maybe_fail("list_cards")
        cards = [c for c in self.cards.values() if status is None or c.status == status]
        return [
            TodoCardSummaryV1(
                card_id=c.card_id,
                title=c.title,
                status=c.status,
                created_ts=c.created_ts,
                updated_ts=c.updated_ts,
                tags=list(c.tags),
                pinned=c.pinned,
            )
            for c in cards[:limit]
        ]

    async def list_card_ids(self) -> list[str]:
        self._maybe_fail("list_card_ids")
        return list(self.cards)

    # -- objectives --------------------------------------------------------

    async def add_objective(self, obj: TodoObjectiveV1) -> bool:
        self._maybe_fail("add_objective")
        self.objectives[obj.objective_id] = obj
        return True

    async def get_objective(self, objective_id: str) -> TodoObjectiveV1 | None:
        self._maybe_fail("get_objective")
        return self.objectives.get(objective_id)

    async def update_objective(
        self, objective_id: str, fields: dict[str, Any]
    ) -> TodoObjectiveV1 | None:
        self._maybe_fail("update_objective")
        obj = self.objectives.get(objective_id)
        if obj is None:
            return None
        for key, value in fields.items():
            setattr(obj, key, value)
        return obj

    async def delete_objective(self, objective_id: str) -> TodoObjectiveV1 | None:
        self._maybe_fail("delete_objective")
        return self.objectives.pop(objective_id, None)

    async def assign_note(
        self, note_id: str, objective_id: str | None, phase: str | None, updated_ts: float
    ) -> TodoNoteV1 | None:
        self._maybe_fail("assign_note")
        note = self.notes.get(note_id)
        if note is None:
            return None
        note.objective_id = objective_id
        note.phase = phase
        note.updated_ts = updated_ts
        return note

    # -- notes -------------------------------------------------------------

    async def add_note(self, note: TodoNoteV1) -> bool:
        self._maybe_fail("add_note")
        self.notes[note.note_id] = note
        return True

    async def update_note(self, note_id: str, body: str, updated_ts: float) -> TodoNoteV1 | None:
        self._maybe_fail("update_note")
        note = self.notes.get(note_id)
        if note is None:
            return None
        note.body = body
        note.updated_ts = updated_ts
        return note

    async def delete_note(self, note_id: str) -> bool:
        self._maybe_fail("delete_note")
        return self.notes.pop(note_id, None) is not None

    # -- attachments -------------------------------------------------------

    async def add_attachment(self, att: TodoAttachmentV1) -> bool:
        self._maybe_fail("add_attachment")
        self.attachments[att.attachment_id] = att
        return True

    async def update_attachment(self, att: TodoAttachmentV1) -> bool:
        self._maybe_fail("update_attachment")
        if att.attachment_id not in self.attachments:
            return False
        self.attachments[att.attachment_id] = att
        return True

    async def delete_attachment(self, attachment_id: str) -> TodoAttachmentV1 | None:
        self._maybe_fail("delete_attachment")
        return self.attachments.pop(attachment_id, None)

    async def list_all_attachments(self, *, limit: int = 200) -> list[TodoAttachmentV1]:
        self._maybe_fail("list_all_attachments")
        return list(self.attachments.values())[:limit]

    # -- events ------------------------------------------------------------

    async def add_event(self, ev: TodoEventV1) -> None:
        self._maybe_fail("add_event")
        self.events.append(ev)

    async def list_events(self, card_id: str, *, limit: int = 50) -> list[TodoEventV1]:
        self._maybe_fail("list_events")
        return [e for e in self.events if e.card_id == card_id][:limit]

    # -- helpers for assertions -------------------------------------------

    def event_kinds(self, card_id: str | None = None) -> list[str]:
        """The event trail, as kinds — what a test usually wants to assert."""
        return [e.kind for e in self.events if card_id is None or e.card_id == card_id]
