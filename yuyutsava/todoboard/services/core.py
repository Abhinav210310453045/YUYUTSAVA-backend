"""What every board service needs: the store, the guard, the timeline, and the
two parent lookups.

The four domain services never import each other. That is not a style rule —
it is what keeps the board's write path from collapsing back into one object.
It works because the things they would otherwise call each other for are here:

* the **store**, resolved lazily so an exchange built before boot wiring still
  lands on the store the daemon injected;
* :meth:`guard`, which turns any backend fault into ``TodoStorageError``;
* :meth:`emit` and :meth:`events`, the timeline — cross-domain by nature, since
  every domain writes to it;
* :meth:`require_card` and :meth:`require_objective`, load-or-404 for the two
  parents that children hang off. A note validating its objective is not the
  notes service asking the objectives service anything; it is one row lookup.

Genuinely cross-domain *work* — creating a card's first note — is not here
either. That is the facade's job, because it is a decision about ordering two
domains, not a primitive.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from ulid import ULID

from yuyutsava.storage.paths import blobs_dir
from yuyutsava.todoboard.errors import TodoError, TodoNotFoundError, TodoStorageError
from yuyutsava.todoboard.models import EVENT_ACTORS, TodoCardV1, TodoEventV1, TodoObjectiveV1
from yuyutsava.todoboard.store import TodoStore
from yuyutsava.todoboard.validate import limit as check_limit

logger = logging.getLogger("yuyutsava.todoboard.exchange")


def workspace_root() -> Path:
    """Root of every card workspace (``blobs/todoboard/``)."""
    return blobs_dir() / "todoboard"


class BoardCore:
    """One store, reached safely."""

    def __init__(self, store: TodoStore, note_index: Any | None = None) -> None:
        self.store = store
        #: ``TodoNoteIndex`` for semantic recall and embed-on-write; ``None``
        #: where there is none (SQLite-only), and every hook no-ops.
        self.note_index = note_index

    async def guard(self, coro, action: str):
        """Run one store call, wrapping backend faults in TodoStorageError."""
        try:
            return await coro
        except TodoError:
            raise
        except Exception as exc:  # noqa: BLE001 — backend fault, typed for callers
            raise TodoStorageError(f"failed to {action}: {exc}") from exc

    # -- the two parents ---------------------------------------------------

    async def require_card(self, card_id: str) -> TodoCardV1:
        """The hydrated card, or 404. Every child write calls this first, so a
        bad id is refused before anything destructive or partial happens."""
        card = await self.guard(self.store.get_card(card_id), "load card")
        if card is None:
            raise TodoNotFoundError(f"no TODO card with id {card_id!r}")
        return card

    async def require_objective(self, objective_id: str) -> TodoObjectiveV1:
        obj = await self.guard(self.store.get_objective(objective_id), "load objective")
        if obj is None:
            raise TodoNotFoundError(f"no TODO objective with id {objective_id!r}")
        return obj

    # -- the timeline ------------------------------------------------------

    async def emit(
        self,
        card_id: str,
        kind: str,
        *,
        objective_id: str | None = None,
        payload: dict[str, Any] | None = None,
        actor: str = "user",
    ) -> None:
        """Append one timeline event, best-effort — like the note index, an
        event write must never fail the mutation it describes."""
        try:
            await self.store.add_event(
                TodoEventV1(
                    event_id=f"tde_{ULID()}",
                    card_id=card_id,
                    objective_id=objective_id,
                    kind=kind,
                    payload={k: v for k, v in (payload or {}).items() if v is not None},
                    actor=actor if actor in EVENT_ACTORS else "system",
                    created_ts=time.time(),
                )
            )
        except Exception:  # noqa: BLE001 — history is an enhancement, never a gate
            logger.debug("todo: event emit failed (%s on %s)", kind, card_id, exc_info=True)

    async def events(self, card_id: str, *, limit: int = 500) -> list[TodoEventV1]:
        """A card's activity timeline, oldest first — feeds the journey doc
        and the card view's activity strip."""
        check_limit(limit)
        return await self.guard(self.store.list_events(card_id, limit=limit), "list events")


__all__ = ["BoardCore", "workspace_root"]
