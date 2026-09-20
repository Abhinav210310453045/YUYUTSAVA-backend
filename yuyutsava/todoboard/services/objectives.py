"""Objectives: a card's think flow.

Phase transitions are free-form (any → any) — the timeline records every move,
so a mistake is recoverable without a transition matrix fighting the tools and
the UI.
"""

from __future__ import annotations

import time
from typing import Any

from ulid import ULID

from yuyutsava.todoboard import validate
from yuyutsava.todoboard.errors import TodoNotFoundError
from yuyutsava.todoboard.models import TodoObjectiveV1
from yuyutsava.todoboard.services.core import BoardCore


class ObjectiveService:
    """Append, patch and drop the objectives on a card."""

    def __init__(self, core: BoardCore) -> None:
        self._core = core

    async def add(
        self,
        card_id: str,
        title: str,
        *,
        phase: str = "thinking",
        reason: str | None = None,
        actor: str = "user",
    ) -> TodoObjectiveV1:
        """Append one objective to a card's think flow (order_idx = end)."""
        title = validate.title(title, "objective")
        validate.phase(phase)
        reason = validate.reason(reason)
        # Appending needs the current tail; boards are small and the hydrated
        # read doubles as the card-exists check (404 before any write).
        card = await self._core.require_card(card_id)
        order_idx = max((o.order_idx for o in card.objectives), default=-1) + 1
        now = time.time()
        obj = TodoObjectiveV1(
            objective_id=f"tob_{ULID()}",
            card_id=card_id,
            title=title,
            phase=phase,
            order_idx=order_idx,
            reason=reason,
            created_ts=now,
            updated_ts=now,
        )
        if not await self._core.guard(self._core.store.add_objective(obj), "add objective"):
            raise TodoNotFoundError(f"no TODO card with id {card_id!r}")
        await self._core.emit(
            card_id,
            "objective_created",
            objective_id=obj.objective_id,
            payload={"title": title, "phase": phase},
            actor=actor,
        )
        return obj

    async def update(
        self,
        objective_id: str,
        *,
        title: str | None = None,
        phase: str | None = None,
        order_idx: int | None = None,
        reason: str | None = None,
        outcome: str | None = None,
        actor: str = "user",
    ) -> TodoObjectiveV1:
        old = await self._core.require_objective(objective_id)
        fields = self._patch(
            title=title, phase=phase, order_idx=order_idx, reason=reason, outcome=outcome
        )
        if not fields:
            return old
        fields["updated_ts"] = time.time()
        obj = await self._core.guard(
            self._core.store.update_objective(objective_id, fields), "update objective"
        )
        if obj is None:
            raise TodoNotFoundError(f"no TODO objective with id {objective_id!r}")
        await self._record(old, obj, fields, phase=phase, reason=reason, actor=actor)
        return obj

    @staticmethod
    def _patch(
        *,
        title: str | None,
        phase: str | None,
        order_idx: int | None,
        reason: str | None,
        outcome: str | None,
    ) -> dict[str, Any]:
        """The validated fields a patch actually changes; empty means no-op."""
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = validate.title(title, "objective")
        if phase is not None:
            validate.phase(phase)
            fields["phase"] = phase
        if order_idx is not None:
            fields["order_idx"] = int(order_idx)
        if reason is not None:
            fields["reason"] = validate.reason(reason)
        if outcome is not None:
            fields["outcome"] = validate.reason(outcome)
        return fields

    async def _record(
        self,
        old: TodoObjectiveV1,
        obj: TodoObjectiveV1,
        fields: dict[str, Any],
        *,
        phase: str | None,
        reason: str | None,
        actor: str,
    ) -> None:
        """A phase move and a field edit are different events, and one patch
        can be both."""
        phase_moved = phase is not None and phase != old.phase
        if phase_moved:
            payload = {"from": old.phase, "to": phase, "title": obj.title}
            if reason:
                payload["reason"] = reason
            await self._core.emit(
                obj.card_id,
                "objective_phase",
                objective_id=obj.objective_id,
                payload=payload,
                actor=actor,
            )
        # reason riding along with a phase move is part of the transition
        # (already in the phase payload), not a separate update.
        skip = ("phase", "updated_ts") + (("reason",) if phase_moved else ())
        other = [k for k in fields if k not in skip]
        if other:
            await self._core.emit(
                obj.card_id,
                "objective_updated",
                objective_id=obj.objective_id,
                payload={"fields": other, "title": obj.title},
                actor=actor,
            )

    async def delete(self, objective_id: str, *, actor: str = "user") -> None:
        """Drop one objective; its notes demote to card-level general notes
        (objective_id NULLed), never deleted — notes are the user's thinking,
        the objective was scaffolding."""
        obj = await self._core.guard(
            self._core.store.delete_objective(objective_id), "delete objective"
        )
        if obj is None:
            raise TodoNotFoundError(f"no TODO objective with id {objective_id!r}")
        await self._core.emit(
            obj.card_id,
            "objective_deleted",
            objective_id=objective_id,
            payload={"title": obj.title, "phase": obj.phase},
            actor=actor,
        )


__all__ = ["ObjectiveService"]
