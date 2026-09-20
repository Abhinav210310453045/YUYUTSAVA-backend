"""Attachments: the files and links a card carries, and the blocks that make them.

Validation is not written here. It is dispatched to the artifact-block registry
by ``(kind, mime)``, so a new attachment family is a new block rather than
another branch in this file.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from ulid import ULID

from yuyutsava.todoboard import validate
from yuyutsava.todoboard.errors import (
    TodoAttachmentError,
    TodoError,
    TodoNotFoundError,
    TodoValidationError,
    require,
)
from yuyutsava.todoboard.models import TodoAttachmentV1
from yuyutsava.todoboard.services.core import BoardCore, logger, workspace_root


class AttachmentService:
    """Record, generate, list and drop a card's attachments."""

    def __init__(self, core: BoardCore) -> None:
        self._core = core

    async def attach(
        self,
        card_id: str,
        kind: str,
        *,
        path: str | None = None,
        url: str | None = None,
        mime: str | None = None,
        title: str | None = None,
        meta: dict[str, Any] | None = None,
        actor: str = "user",
    ) -> TodoAttachmentV1:
        """Record an attachment on a card. ``link`` kinds carry a URL,
        file-backed kinds reference an existing file on disk (the upload
        endpoint / agent tools write the file into the card workspace first).
        The block may also infer a missing mime from the file suffix."""
        # Deferred for cost, not for a cycle: artifacts.py reaches the TTS and
        # visual renderers, a measured 27 ms and 169 modules that every board
        # consumer would otherwise pay to attach a file.
        from yuyutsava.todoboard.artifacts import resolve_block  # noqa: PLC0415

        validate.kind(kind)
        block = resolve_block(kind, mime)
        # Validators touch the filesystem — off the event loop.
        mime = await asyncio.to_thread(block.validate, path=path, url=url, mime=mime)
        att = TodoAttachmentV1(
            attachment_id=f"tda_{ULID()}",
            card_id=card_id,
            kind=kind,
            path=path,
            url=url,
            mime=mime,
            title=title,
            meta=meta or {},
            created_ts=time.time(),
        )
        if not await self._core.guard(self._core.store.add_attachment(att), "add attachment"):
            raise TodoNotFoundError(f"no TODO card with id {card_id!r}")
        block_name = (meta or {}).get("block")
        await self._core.emit(
            card_id,
            self._event_kind(block_name),
            payload={
                "attachment_id": att.attachment_id,
                "kind": kind,
                "title": title,
                **({"block": block_name} if block_name else {}),
            },
            actor=actor,
        )
        return att

    async def generate(
        self,
        card_id: str,
        block: str,
        spec: dict[str, Any] | None = None,
        *,
        title: str | None = None,
        actor: str = "user",
    ) -> TodoAttachmentV1:
        """Generate an artifact into the card workspace and attach it — the
        one shared path behind the todo_generate_artifact tool and the REST
        generate endpoint."""
        require(isinstance(spec, dict) or spec is None, "spec must be a JSON object")
        gen = self._generative_block(block)
        card = await self._core.require_card(card_id)
        out_dir = Path(card.workspace_path) if card.workspace_path else workspace_root() / card_id
        spec = dict(spec or {})
        if gen.needs_context:
            # Injected HERE, on the event loop: generators run in a thread and
            # must never call back into the loop-bound store.
            events = await self._core.events(card_id)
            spec["card"] = card.model_dump()
            spec["events"] = [e.model_dump() for e in events]
        # Generators do blocking work (TTS/renderers) — off the event loop,
        # like validators.
        try:
            path, mime = await asyncio.to_thread(gen.generate, spec, out_dir)
        except TodoError:
            raise
        except Exception as exc:  # a broken generator must not crash the loop
            raise TodoAttachmentError(f"{block} generation failed: {exc}") from exc
        if gen.singleton:
            existing = next(
                (a for a in card.attachments if (a.meta or {}).get("block") == block),
                None,
            )
            if existing is not None:
                return await self._replace(
                    existing, path=str(path), mime=mime, title=title, block=block, actor=actor
                )
        return await self.attach(
            card_id,
            gen.kind,
            path=str(path),
            mime=mime,
            title=title,
            meta={"source": "generate", "block": block, "author": actor},
            actor=actor,
        )

    @staticmethod
    def _generative_block(block: str):
        """The named block, or a validation error listing the ones that exist."""
        # Deferred for cost — see attach().
        from yuyutsava.todoboard.artifacts import blocks  # noqa: PLC0415

        gen = next((b for b in blocks() if b.name == block), None)
        if gen is None or gen.generate is None:
            names = ", ".join(sorted(b.name for b in blocks() if b.generate))
            raise TodoValidationError(
                f"no generative artifact block named {block!r} (available: {names})"
            )
        return gen

    async def _replace(
        self,
        existing: TodoAttachmentV1,
        *,
        path: str,
        mime: str,
        title: str | None,
        block: str,
        actor: str,
    ) -> TodoAttachmentV1:
        """Refresh a singleton block's attachment in place: point the row at
        the freshly generated file, then unlink the superseded one. New file
        first, row swap second, old-file unlink last — a crash mid-way always
        leaves a working attachment, never a dangling row."""
        old_path = existing.path
        updated = existing.model_copy(
            update={
                "path": path,
                "mime": mime,
                "title": title if title is not None else existing.title,
                "meta": {**(existing.meta or {}), "author": actor},
                "created_ts": time.time(),
            }
        )
        if not await self._core.guard(
            self._core.store.update_attachment(updated), "update attachment"
        ):
            raise TodoNotFoundError(f"no TODO attachment with id {existing.attachment_id!r}")
        if old_path and old_path != path and Path(old_path).is_relative_to(workspace_root()):
            await self._unlink(old_path, "superseded artifact")
        await self._core.emit(
            updated.card_id,
            self._event_kind(block),
            payload={
                "attachment_id": updated.attachment_id,
                "kind": updated.kind,
                "title": updated.title,
                "block": block,
                "updated": True,
            },
            actor=actor,
        )
        return updated

    async def delete(self, attachment_id: str) -> None:
        """Drop the row and, when the file lives inside the card workspace,
        the file itself (files elsewhere aren't ours to delete)."""
        att = await self._core.guard(
            self._core.store.delete_attachment(attachment_id), "delete attachment"
        )
        if att is None:
            raise TodoNotFoundError(f"no TODO attachment with id {attachment_id!r}")
        if att.path and Path(att.path).is_relative_to(workspace_root()):
            await self._unlink(att.path, "attachment file")

    async def list_all(self, *, limit: int = 200) -> list[TodoAttachmentV1]:
        """Every card's attachments, newest first — feeds the global gallery
        (the Artifacts view) so board files show up next to chat artifacts."""
        return await self._core.guard(
            self._core.store.list_all_attachments(limit=limit), "list attachments"
        )

    @staticmethod
    def _event_kind(block: str | None) -> str:
        """The journey doc is the card's own story, so it gets its own kind —
        the UI files it under the timeline rather than the artifact strip."""
        return "journey_generated" if block == "journey" else "artifact_attached"

    @staticmethod
    async def _unlink(path: str, what: str) -> None:
        """Best-effort: a file we cannot remove is a sweeper's problem, not a
        reason to fail a write that already succeeded."""
        try:
            await asyncio.to_thread(Path(path).unlink, missing_ok=True)
        except OSError:
            logger.warning("todo: could not unlink %s %s", what, path)


__all__ = ["AttachmentService"]
