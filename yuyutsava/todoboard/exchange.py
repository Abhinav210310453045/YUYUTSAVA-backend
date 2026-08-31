"""The TODO-board exchange protocol — the ONLY write/read path to the board.

Every producer/consumer (REST router, ``todo_*`` tools, master agent,
TinkerAgent) talks to :class:`TodoExchange`; nothing else may touch the store
or its tables. The exchange owns:

  * validation (status/author/kind vocabularies, title/body limits) — raising
    :class:`TodoValidationError` before the store ever sees bad data;
  * the per-card **workspace** under ``blobs_dir()/todoboard/<card_id>/`` —
    created on ``add_card``, removed on ``delete_card`` (rows first via
    CASCADE, then the dir; a re-run after a partial failure is a no-op);
  * typed exceptions so callers map failures deterministically (the REST layer
    → HTTP status codes, tools → structured error strings).

Storage faults are wrapped in :class:`TodoStorageError` so agent loops and
routers never crash on a backend hiccup.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from ulid import ULID

from yuyutsava.storage.paths import blobs_dir
from yuyutsava.todoboard.models import (
    ATTACHMENT_KINDS,
    CARD_STATUSES,
    EVENT_ACTORS,
    MAX_NOTE_LEN,
    MAX_REASON_LEN,
    MAX_TITLE_LEN,
    NOTE_AUTHORS,
    OBJECTIVE_PHASES,
    BoardSnapshotV1,
    TodoAttachmentV1,
    TodoCardSummaryV1,
    TodoCardV1,
    TodoEventV1,
    TodoNoteV1,
    TodoObjectiveV1,
)
from yuyutsava.todoboard.store import (
    DEFAULT_LIST_LIMIT,
    TodoStore,
    get_default_todo_store,
)

logger = logging.getLogger("yuyutsava.todoboard.exchange")


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


def _workspace_root() -> Path:
    return blobs_dir() / "todoboard"


def board_workspace_root() -> Path:
    """Root of every card workspace (``blobs/todoboard/``) — the containment
    zone for agents that work across cards (the background TinkerAgent)."""
    return _workspace_root()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TodoValidationError(message)


class TodoExchange:
    """Versioned protocol facade over one :class:`TodoStore`."""

    def __init__(self, store: TodoStore | None = None) -> None:
        # None = resolve the process default lazily on every call, so an
        # exchange built before daemon/CLI boot wiring still ends up on the
        # store set_default_todo_store() injected.
        self._injected = store

    @property
    def _store(self) -> TodoStore:
        return self._injected or get_default_todo_store()

    # ── cards ──────────────────────────────────────────────────────────

    async def add_card(
        self,
        title: str,
        *,
        status: str = "inbox",
        tags: list[str] | None = None,
        pinned: bool = False,
        note: str | None = None,
        note_author: str = "user",
    ) -> TodoCardV1:
        """Create a card (optionally with a first note) and its workspace dir."""
        title = (title or "").strip()
        _require(bool(title), "card title must be non-empty")
        _require(len(title) <= MAX_TITLE_LEN, f"card title exceeds {MAX_TITLE_LEN} chars")
        self._check_status(status)
        tags = self._check_tags(tags)

        card_id = f"tdo_{ULID()}"
        workspace = _workspace_root() / card_id
        now = time.time()
        card = TodoCardV1(
            card_id=card_id, title=title, status=status, pinned=bool(pinned),
            tags=tags, workspace_path=str(workspace),
            created_ts=now, updated_ts=now,
        )
        # Workspace dir first, row second: a row without a workspace would break
        # tr_*/artifact writes later, while an orphan dir is swept harmlessly.
        try:
            await asyncio.to_thread(workspace.mkdir, parents=True, exist_ok=True)
        except OSError as exc:
            raise TodoAttachmentError(f"cannot create card workspace: {exc}") from exc
        try:
            await self._store.add_card(card)
        except TodoError:
            raise
        except Exception as exc:  # noqa: BLE001 — backend fault, typed for callers
            await asyncio.to_thread(shutil.rmtree, workspace, True)
            raise TodoStorageError(f"failed to persist card: {exc}") from exc
        if note:
            card.notes.append(await self.add_note(card_id, note, author=note_author))
        return card

    async def get_card(self, card_id: str) -> TodoCardV1:
        card = await self._guard(self._store.get_card(card_id), "load card")
        if card is None:
            raise TodoNotFoundError(f"no TODO card with id {card_id!r}")
        return card

    async def update_card(
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
            title = title.strip()
            _require(bool(title), "card title must be non-empty")
            _require(len(title) <= MAX_TITLE_LEN, f"card title exceeds {MAX_TITLE_LEN} chars")
            fields["title"] = title
        old_status: str | None = None
        if status is not None:
            self._check_status(status)
            # Read-before-write only when the status might move: the timeline
            # records transitions, not writes, so a no-op change emits nothing.
            old_status = (await self.get_card(card_id)).status
            fields["status"] = status
        if pinned is not None:
            fields["pinned"] = bool(pinned)
        if tags is not None:
            fields["tags"] = self._check_tags(tags)
        if fields:
            fields["updated_ts"] = time.time()
            found = await self._guard(
                self._store.update_card(card_id, fields), "update card"
            )
            if not found:
                raise TodoNotFoundError(f"no TODO card with id {card_id!r}")
            if status is not None and old_status is not None and status != old_status:
                await self._emit(
                    card_id, "card_status",
                    payload={"from": old_status, "to": status}, actor=actor,
                )
        return await self.get_card(card_id)

    async def delete_card(self, card_id: str) -> None:
        """Drop the card, its children (CASCADE), and its workspace dir."""
        card = await self.get_card(card_id)  # 404 before any destructive step
        await self._guard(self._store.delete_card(card_id), "delete card")
        if card.workspace_path:
            try:
                await asyncio.to_thread(shutil.rmtree, card.workspace_path, True)
            except OSError:
                logger.warning("todo: could not remove workspace %s", card.workspace_path)

    async def query_board(
        self,
        *,
        status: str | None = None,
        tag: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[TodoCardSummaryV1]:
        if status is not None:
            self._check_status(status)
        _require(1 <= limit <= 5_000, "limit must be between 1 and 5000")
        cards = await self._guard(
            self._store.list_cards(status=status, limit=limit), "list cards"
        )
        if tag:
            # Boards are small (hundreds of cards); tag filtering in Python keeps
            # the twin stores' SQL identical instead of forking on JSON operators.
            cards = [c for c in cards if tag in c.tags]
        return cards

    async def board_snapshot(self) -> BoardSnapshotV1:
        return BoardSnapshotV1(cards=await self.query_board())

    async def list_card_ids(self) -> list[str]:
        """Every card id on the board, unfiltered — the sweeper's ground truth
        for orphan-dir detection (a dir under blobs/todoboard/ with no id here
        is an orphan)."""
        return await self._guard(self._store.list_card_ids(), "list card ids")

    # ── objectives (think flow) ────────────────────────────────────────

    async def add_objective(
        self,
        card_id: str,
        title: str,
        *,
        phase: str = "thinking",
        reason: str | None = None,
        actor: str = "user",
    ) -> TodoObjectiveV1:
        """Append one objective to a card's think flow (order_idx = end)."""
        title = (title or "").strip()
        _require(bool(title), "objective title must be non-empty")
        _require(len(title) <= MAX_TITLE_LEN, f"objective title exceeds {MAX_TITLE_LEN} chars")
        self._check_phase(phase)
        reason = self._check_reason(reason)
        # Appending needs the current tail; boards are small and the hydrated
        # read doubles as the card-exists check (404 before any write).
        card = await self.get_card(card_id)
        order_idx = max((o.order_idx for o in card.objectives), default=-1) + 1
        now = time.time()
        obj = TodoObjectiveV1(
            objective_id=f"tob_{ULID()}", card_id=card_id, title=title,
            phase=phase, order_idx=order_idx, reason=reason,
            created_ts=now, updated_ts=now,
        )
        if not await self._guard(self._store.add_objective(obj), "add objective"):
            raise TodoNotFoundError(f"no TODO card with id {card_id!r}")
        await self._emit(
            card_id, "objective_created", objective_id=obj.objective_id,
            payload={"title": title, "phase": phase}, actor=actor,
        )
        return obj

    async def update_objective(
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
        """Patch an objective. Phase transitions are free-form (any → any) —
        the timeline records every move, so mistakes are recoverable without
        a transition matrix fighting the tools/UI."""
        old = await self._guard(self._store.get_objective(objective_id), "load objective")
        if old is None:
            raise TodoNotFoundError(f"no TODO objective with id {objective_id!r}")
        fields: dict[str, Any] = {}
        if title is not None:
            title = title.strip()
            _require(bool(title), "objective title must be non-empty")
            _require(len(title) <= MAX_TITLE_LEN, f"objective title exceeds {MAX_TITLE_LEN} chars")
            fields["title"] = title
        if phase is not None:
            self._check_phase(phase)
            fields["phase"] = phase
        if order_idx is not None:
            fields["order_idx"] = int(order_idx)
        if reason is not None:
            fields["reason"] = self._check_reason(reason)
        if outcome is not None:
            fields["outcome"] = self._check_reason(outcome)
        if not fields:
            return old
        fields["updated_ts"] = time.time()
        obj = await self._guard(
            self._store.update_objective(objective_id, fields), "update objective"
        )
        if obj is None:
            raise TodoNotFoundError(f"no TODO objective with id {objective_id!r}")
        phase_moved = phase is not None and phase != old.phase
        if phase_moved:
            payload = {"from": old.phase, "to": phase, "title": obj.title}
            if reason:
                payload["reason"] = reason
            await self._emit(
                obj.card_id, "objective_phase", objective_id=objective_id,
                payload=payload, actor=actor,
            )
        # reason riding along with a phase move is part of the transition
        # (already in the phase payload), not a separate update.
        skip = ("phase", "updated_ts") + (("reason",) if phase_moved else ())
        other = [k for k in fields if k not in skip]
        if other:
            await self._emit(
                obj.card_id, "objective_updated", objective_id=objective_id,
                payload={"fields": other, "title": obj.title}, actor=actor,
            )
        return obj

    async def delete_objective(self, objective_id: str, *, actor: str = "user") -> None:
        """Drop one objective; its notes demote to card-level general notes
        (objective_id NULLed), never deleted — notes are the user's thinking,
        the objective was scaffolding."""
        obj = await self._guard(
            self._store.delete_objective(objective_id), "delete objective"
        )
        if obj is None:
            raise TodoNotFoundError(f"no TODO objective with id {objective_id!r}")
        await self._emit(
            obj.card_id, "objective_deleted", objective_id=objective_id,
            payload={"title": obj.title, "phase": obj.phase}, actor=actor,
        )

    async def assign_note(
        self,
        note_id: str,
        *,
        objective_id: str | None,
        phase: str | None = None,
        actor: str = "user",
    ) -> TodoNoteV1:
        """Attach a note to an objective (or clear with objective_id=None).
        No reindex — the body is unchanged, only the assignment moves."""
        if phase is not None:
            self._check_phase(phase)
        if objective_id is not None:
            obj = await self._guard(
                self._store.get_objective(objective_id), "load objective"
            )
            if obj is None:
                raise TodoNotFoundError(f"no TODO objective with id {objective_id!r}")
            # Cross-card guard BEFORE the write: the objective's hydrated card
            # already carries its notes, so membership is one read.
            card = await self.get_card(obj.card_id)
            _require(
                any(n.note_id == note_id for n in card.notes),
                f"objective {objective_id} lives on card {obj.card_id}, "
                f"which has no note {note_id!r}",
            )
        note = await self._guard(
            self._store.assign_note(note_id, objective_id, phase, time.time()),
            "assign note",
        )
        if note is None:
            raise TodoNotFoundError(f"no TODO note with id {note_id!r}")
        if objective_id is not None:
            await self._emit(
                note.card_id, "note_assigned", objective_id=objective_id,
                payload={"note_id": note_id, "phase": phase}, actor=actor,
            )
        return note

    async def list_events(self, card_id: str, *, limit: int = 500) -> list[TodoEventV1]:
        """A card's activity timeline, oldest first — feeds the journey doc
        and the card view's activity strip."""
        _require(1 <= limit <= 5_000, "limit must be between 1 and 5000")
        return await self._guard(
            self._store.list_events(card_id, limit=limit), "list events"
        )

    # ── notes ──────────────────────────────────────────────────────────

    async def add_note(
        self,
        card_id: str,
        body: str,
        *,
        author: str = "user",
        objective_id: str | None = None,
        phase: str | None = None,
    ) -> TodoNoteV1:
        body = (body or "").strip()
        _require(bool(body), "note body must be non-empty")
        _require(len(body) <= MAX_NOTE_LEN, f"note body exceeds {MAX_NOTE_LEN} chars")
        _require(author in NOTE_AUTHORS, f"author must be one of {NOTE_AUTHORS}")
        if phase is not None:
            self._check_phase(phase)
        if objective_id is not None:
            obj = await self._guard(
                self._store.get_objective(objective_id), "load objective"
            )
            if obj is None:
                raise TodoNotFoundError(f"no TODO objective with id {objective_id!r}")
            _require(
                obj.card_id == card_id,
                f"objective {objective_id} lives on card {obj.card_id}, not {card_id}",
            )
        now = time.time()
        note = TodoNoteV1(
            note_id=f"tdn_{ULID()}", card_id=card_id, body=body,
            author=author, objective_id=objective_id, phase=phase,
            created_ts=now, updated_ts=now,
        )
        if not await self._guard(self._store.add_note(note), "add note"):
            raise TodoNotFoundError(f"no TODO card with id {card_id!r}")
        self._schedule_note_index(note)
        if objective_id is not None:
            await self._emit(
                card_id, "note_assigned", objective_id=objective_id,
                payload={"note_id": note.note_id, "phase": phase}, actor=author,
            )
        return note

    async def update_note(self, note_id: str, body: str) -> TodoNoteV1:
        body = (body or "").strip()
        _require(bool(body), "note body must be non-empty")
        _require(len(body) <= MAX_NOTE_LEN, f"note body exceeds {MAX_NOTE_LEN} chars")
        note = await self._guard(
            self._store.update_note(note_id, body, time.time()), "update note"
        )
        if note is None:
            raise TodoNotFoundError(f"no TODO note with id {note_id!r}")
        self._schedule_note_index(note, replace=True)
        return note

    async def search_notes(
        self, query: str, *, k: int = 8, card_id: str | None = None
    ) -> list:
        """Semantic recall over note bodies (``todo_note_chunks``), optionally
        scoped to one card. Returns retrieval ``Hit``s whose payload carries
        ``card_id``/``note_id``. Empty when no index is wired (SQLite-only
        deployments) — recall is an enhancement, never a dependency."""
        query = (query or "").strip()
        _require(bool(query), "search query must be non-empty")
        _require(1 <= k <= 50, "k must be between 1 and 50")
        from yuyutsava.todoboard.recall import get_default_note_index

        index = get_default_note_index()
        if index is None or not index.enabled:
            return []
        return await index.search(query, k, card_id=card_id)

    async def delete_note(self, note_id: str) -> None:
        if not await self._guard(self._store.delete_note(note_id), "delete note"):
            raise TodoNotFoundError(f"no TODO note with id {note_id!r}")

    # ── attachments ────────────────────────────────────────────────────

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
        """Record an attachment on a card. Validation is dispatched to the
        artifact-block registry by (kind, mime): ``link`` kinds carry a URL,
        file-backed kinds reference an existing file on disk (the upload
        endpoint / agent tools write the file into the card workspace first).
        The block may also infer a missing mime from the file suffix."""
        # Lazy import — artifacts.py imports our exception types at module
        # level, so the cycle must break on this side.
        from yuyutsava.todoboard.artifacts import resolve_block

        _require(kind in ATTACHMENT_KINDS, f"kind must be one of {ATTACHMENT_KINDS}")
        block = resolve_block(kind, mime)
        # Validators touch the filesystem — off the event loop.
        mime = await asyncio.to_thread(block.validate, path=path, url=url, mime=mime)
        att = TodoAttachmentV1(
            attachment_id=f"tda_{ULID()}", card_id=card_id, kind=kind,
            path=path, url=url, mime=mime, title=title, meta=meta or {},
            created_ts=time.time(),
        )
        if not await self._guard(self._store.add_attachment(att), "add attachment"):
            raise TodoNotFoundError(f"no TODO card with id {card_id!r}")
        block_name = (meta or {}).get("block")
        await self._emit(
            card_id,
            "journey_generated" if block_name == "journey" else "artifact_attached",
            payload={
                "attachment_id": att.attachment_id, "kind": kind,
                "title": title, **({"block": block_name} if block_name else {}),
            },
            actor=actor,
        )
        return att

    async def generate_artifact(
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
        generate endpoint. Dispatches to a generative block by name; blocks
        with ``needs_context`` get the hydrated card + timeline injected into
        their spec HERE, on the event loop — generators run in a thread and
        must never call back into the loop-bound store."""
        from yuyutsava.todoboard.artifacts import blocks

        _require(isinstance(spec, dict) or spec is None, "spec must be a JSON object")
        gen = next((b for b in blocks() if b.name == block), None)
        if gen is None or gen.generate is None:
            names = ", ".join(sorted(b.name for b in blocks() if b.generate))
            raise TodoValidationError(
                f"no generative artifact block named {block!r} (available: {names})"
            )
        card = await self.get_card(card_id)
        out_dir = (
            Path(card.workspace_path) if card.workspace_path
            else _workspace_root() / card_id
        )
        spec = dict(spec or {})
        if gen.needs_context:
            events = await self.list_events(card_id)
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
                (
                    a for a in card.attachments
                    if (a.meta or {}).get("block") == block
                ),
                None,
            )
            if existing is not None:
                return await self._update_generated(
                    existing, path=str(path), mime=mime, title=title,
                    block=block, actor=actor,
                )
        return await self.attach(
            card_id, gen.kind, path=str(path), mime=mime, title=title,
            meta={"source": "generate", "block": block, "author": actor},
            actor=actor,
        )

    async def _update_generated(
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
        updated = existing.model_copy(update={
            "path": path,
            "mime": mime,
            "title": title if title is not None else existing.title,
            "meta": {**(existing.meta or {}), "author": actor},
            "created_ts": time.time(),
        })
        if not await self._guard(
            self._store.update_attachment(updated), "update attachment"
        ):
            raise TodoNotFoundError(
                f"no TODO attachment with id {existing.attachment_id!r}"
            )
        if (
            old_path
            and old_path != path
            and Path(old_path).is_relative_to(_workspace_root())
        ):
            try:
                await asyncio.to_thread(Path(old_path).unlink, missing_ok=True)
            except OSError:
                logger.warning(
                    "todo: could not unlink superseded artifact %s", old_path
                )
        await self._emit(
            updated.card_id,
            "journey_generated" if block == "journey" else "artifact_attached",
            payload={
                "attachment_id": updated.attachment_id, "kind": updated.kind,
                "title": updated.title, "block": block, "updated": True,
            },
            actor=actor,
        )
        return updated

    async def delete_attachment(self, attachment_id: str) -> None:
        """Drop the row and, when the file lives inside the card workspace,
        the file itself (files elsewhere aren't ours to delete)."""
        att = await self._guard(
            self._store.delete_attachment(attachment_id), "delete attachment"
        )
        if att is None:
            raise TodoNotFoundError(f"no TODO attachment with id {attachment_id!r}")
        if att.path and Path(att.path).is_relative_to(_workspace_root()):
            try:
                await asyncio.to_thread(Path(att.path).unlink, missing_ok=True)
            except OSError:
                logger.warning("todo: could not unlink attachment file %s", att.path)

    async def list_all_attachments(self, *, limit: int = 200) -> list[TodoAttachmentV1]:
        """Every card's attachments, newest first — feeds the global gallery
        (the Artifacts view) so board files show up next to chat artifacts."""
        return await self._guard(
            self._store.list_all_attachments(limit=limit), "list attachments"
        )

    # ── internals ──────────────────────────────────────────────────────

    async def _emit(
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
            await self._store.add_event(TodoEventV1(
                event_id=f"tde_{ULID()}", card_id=card_id,
                objective_id=objective_id, kind=kind,
                payload={k: v for k, v in (payload or {}).items() if v is not None},
                actor=actor if actor in EVENT_ACTORS else "system",
                created_ts=time.time(),
            ))
        except Exception:  # noqa: BLE001 — history is an enhancement, never a gate
            logger.debug("todo: event emit failed (%s on %s)", kind, card_id, exc_info=True)

    def _schedule_note_index(self, note: TodoNoteV1, *, replace: bool = False) -> None:
        """Embed a written note into the recall index, best-effort.

        The index is a retrieval shadow of the board, so the write hook lives
        here — inside the only write path — rather than in callers. A missing
        index (SQLite mode) or degraded Postgres is a silent skip; the boot
        sync repairs the gap. Lazy import keeps recall.py off the import path
        for consumers that never touch it."""
        try:
            from yuyutsava.todoboard.recall import get_default_note_index

            index = get_default_note_index()
            if index is not None:
                index.schedule(note, replace=replace)
        except Exception:  # noqa: BLE001 — indexing must never break a write
            logger.debug("todo: note index scheduling failed", exc_info=True)

    async def _guard(self, coro, action: str):
        """Run one store call, wrapping backend faults in TodoStorageError."""
        try:
            return await coro
        except TodoError:
            raise
        except Exception as exc:  # noqa: BLE001 — backend fault, typed for callers
            raise TodoStorageError(f"failed to {action}: {exc}") from exc

    @staticmethod
    def _check_status(status: str) -> None:
        _require(status in CARD_STATUSES, f"status must be one of {CARD_STATUSES}")

    @staticmethod
    def _check_phase(phase: str) -> None:
        _require(phase in OBJECTIVE_PHASES, f"phase must be one of {OBJECTIVE_PHASES}")

    @staticmethod
    def _check_reason(text: str | None) -> str | None:
        if text is None:
            return None
        text = text.strip()
        _require(len(text) <= MAX_REASON_LEN, f"text exceeds {MAX_REASON_LEN} chars")
        return text or None

    @staticmethod
    def _check_tags(tags: list[str] | None) -> list[str]:
        tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        _require(len(tags) <= 50, "at most 50 tags per card")
        return tags


# Process-singleton over the default store, for callers (tools, routers) that
# don't inject their own. The store singleton underneath is what the daemon/CLI
# swap at boot, so this needs no set_ counterpart.
_default_exchange: TodoExchange | None = None


def get_default_exchange() -> TodoExchange:
    global _default_exchange
    if _default_exchange is None:
        _default_exchange = TodoExchange()
    return _default_exchange


__all__ = [
    "TodoExchange",
    "board_workspace_root",
    "TodoError",
    "TodoValidationError",
    "TodoNotFoundError",
    "TodoStorageError",
    "TodoAttachmentError",
    "get_default_exchange",
]
