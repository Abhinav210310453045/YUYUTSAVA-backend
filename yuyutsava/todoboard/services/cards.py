"""Cards: the board's top-level rows and their workspaces.

A card is the only row with something on disk behind it, so this is the only
service that creates and removes directories.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from typing import Any

from ulid import ULID

from yuyutsava.todoboard import validate
from yuyutsava.todoboard.errors import (
    TodoAttachmentError,
    TodoError,
    TodoNotFoundError,
    TodoStorageError,
)
from yuyutsava.todoboard.models import BoardSnapshotV1, TodoCardSummaryV1, TodoCardV1
from yuyutsava.todoboard.services.core import BoardCore, logger, workspace_root
from yuyutsava.todoboard.store import DEFAULT_LIST_LIMIT


class CardService:
    """Create, read, patch and delete cards."""

    def __init__(self, core: BoardCore) -> None:
        self._core = core

    async def add(
        self,
        title: str,
        *,
        status: str = "inbox",
        tags: list[str] | None = None,
        pinned: bool = False,
    ) -> TodoCardV1:
        """Create a card and its workspace dir.

        A first note is the facade's business, not ours: it belongs to the
        notes domain, and adding it here is what made this one object.
        """
        title = validate.title(title, "card")
        validate.status(status)
        tags = validate.tags(tags)

        card_id = f"tdo_{ULID()}"
        workspace = workspace_root() / card_id
        now = time.time()
        card = TodoCardV1(
            card_id=card_id,
            title=title,
            status=status,
            pinned=bool(pinned),
            tags=tags,
            workspace_path=str(workspace),
            created_ts=now,
            updated_ts=now,
        )
        # Workspace dir first, row second: a row without a workspace would break
        # tr_*/artifact writes later, while an orphan dir is swept harmlessly.
        try:
            await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        except OSError as exc:
            raise TodoAttachmentError(f"cannot create card workspace: {exc}") from exc
        try:
            await self._core.store.add_card(card)
        except TodoError:
            raise
        except Exception as exc:  # noqa: BLE001 — backend fault, typed for callers
            await asyncio.to_thread(shutil.rmtree, workspace, True)
            raise TodoStorageError(f"failed to persist card: {exc}") from exc
        return card

    async def get(self, card_id: str) -> TodoCardV1:
        return await self._core.require_card(card_id)

    async def update(
        self,
        card_id: str,
        *,
        title: str | None = None,
        status: str | None = None,
        pinned: bool | None = None,
        tags: list[str] | None = None,
        actor: str = "user",
    ) -> TodoCardV1:
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = validate.title(title, "card")
        old_status: str | None = None
        if status is not None:
            validate.status(status)
            # Read-before-write only when the status might move: the timeline
            # records transitions, not writes, so a no-op change emits nothing.
            old_status = (await self._core.require_card(card_id)).status
            fields["status"] = status
        if pinned is not None:
            fields["pinned"] = bool(pinned)
        if tags is not None:
            fields["tags"] = validate.tags(tags)
        if fields:
            fields["updated_ts"] = time.time()
            found = await self._core.guard(
                self._core.store.update_card(card_id, fields), "update card"
            )
            if not found:
                raise TodoNotFoundError(f"no TODO card with id {card_id!r}")
            if status is not None and old_status is not None and status != old_status:
                await self._core.emit(
                    card_id,
                    "card_status",
                    payload={"from": old_status, "to": status},
                    actor=actor,
                )
        return await self._core.require_card(card_id)

    async def delete(self, card_id: str) -> None:
        """Drop the card, its children (CASCADE), and its workspace dir."""
        card = await self._core.require_card(card_id)  # 404 before any destructive step
        await self._core.guard(self._core.store.delete_card(card_id), "delete card")
        if card.workspace_path:
            try:
                await asyncio.to_thread(shutil.rmtree, card.workspace_path, True)
            except OSError:
                logger.warning("todo: could not remove workspace %s", card.workspace_path)

    async def query(
        self,
        *,
        status: str | None = None,
        tag: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[TodoCardSummaryV1]:
        if status is not None:
            validate.status(status)
        validate.limit(limit)
        cards = await self._core.guard(
            self._core.store.list_cards(status=status, limit=limit), "list cards"
        )
        if tag:
            # Boards are small (hundreds of cards); tag filtering in Python keeps
            # the twin stores' SQL identical instead of forking on JSON operators.
            cards = [c for c in cards if tag in c.tags]
        return cards

    async def snapshot(self) -> BoardSnapshotV1:
        return BoardSnapshotV1(cards=await self.query())

    async def list_ids(self) -> list[str]:
        """Every card id on the board, unfiltered — the sweeper's ground truth
        for orphan-dir detection (a dir under blobs/todoboard/ with no id here
        is an orphan)."""
        return await self._core.guard(self._core.store.list_card_ids(), "list card ids")


__all__ = ["CardService"]
