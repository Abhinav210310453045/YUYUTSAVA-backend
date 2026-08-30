"""Persistence for rendered visuals: image bytes on disk + metadata row.

Mirrors the voice-audio convention (:mod:`yuyutsava.storage.voice_store`): the
PNG lives on disk and the DB row holds its absolute path, so the HTTP layer can
serve it by id regardless of where it was written. Two write locations:

  * a tool call passes the agent's ``OUTPUT_DIR`` so the file lands in the user's
    workspace (``_output/visuals/…``) and the CLI can point at it;
  * the REST endpoint passes nothing, so it falls back to the canonical blob dir
    (:func:`yuyutsava.storage.paths.blobs_dir` / ``visuals``).

Retention: visuals are session-scoped user output. ``delete_for_thread`` runs on
session delete; ``delete_older_than`` lets the TTL sweeper age out orphans.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ulid import ULID

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.paths import blobs_dir
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread
from .types import RenderResult

logger = logging.getLogger("yuyutsava.visuals.store")

DEFAULT_LIST_LIMIT = 500
_EXT = {"image/png": "png", "image/svg+xml": "svg"}


@dataclass(frozen=True)
class VisualRecord:
    """One persisted visual's metadata (no image bytes — those live on disk)."""

    visual_id: str
    thread_id: str
    kind: str
    title: str | None
    mime: str
    path: str
    source: str | None
    created_ts: float


class VisualStore(ABC):
    """Interface the delivery layer depends on."""

    @abstractmethod
    async def save(
        self, result: RenderResult, thread_id: str, *, out_dir: str | Path | None = None
    ) -> VisualRecord:
        """Write the image to disk and record its metadata. Returns the record."""

    @abstractmethod
    async def get(self, visual_id: str) -> VisualRecord | None:
        """One record by id — used to serve its image file."""

    @abstractmethod
    async def list_for_thread(
        self, thread_id: str, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[VisualRecord]:
        """Records for a thread, newest first."""

    @abstractmethod
    async def delete(self, visual_id: str) -> bool:
        """Delete one visual everywhere the agent stored it — the metadata row
        and the image file on disk (``rec.path``). A user's own downloaded copy
        lives at a separate, untracked path and is untouched. Returns ``True``
        when a row was removed, ``False`` if the id was unknown."""

    @abstractmethod
    async def delete_for_thread(self, thread_id: str) -> int:
        """Drop rows + image files for a thread. Returns rows deleted."""

    @abstractmethod
    async def delete_older_than(self, cutoff_ts: float) -> int:
        """Drop rows + files older than *cutoff_ts* (TTL sweep)."""


def _blob_dir(out_dir: str | Path | None) -> Path:
    return Path(out_dir) / "visuals" if out_dir else blobs_dir() / "visuals"


# ---------------------------------------------------------------------------
# NOTE: SqliteVisualStore and PgVisualStore lived here until 2026-08-08.
#
# They were the first domain collapsed onto the dialect adapter (ADR-002 step
# 2.3): 211 lines of parallel implementation replaced by the 95-line
# ``UnifiedVisualStore`` in ``store_unified.py``, which serves both backends.
#
# The removal is justified by test/storage/test_visual_store_parity.py, which
# ran the same behavioural contract against the twins AND the unified store on
# both live backends (40 assertions) before they were deleted. Git has them if
# the comparison is ever needed again.
#
# This module keeps the shared vocabulary: VisualRecord, the VisualStore
# interface, the blob-dir helpers, and the process-default accessors below.
# ---------------------------------------------------------------------------


_default_store: "VisualStore | None" = None


def set_default_visual_store(store: VisualStore) -> None:
    global _default_store
    _default_store = store


def get_default_visual_store() -> VisualStore:
    global _default_store
    if _default_store is None:
        from yuyutsava.storage.paths import state_db_path
        from yuyutsava.visuals.store_unified import sqlite_visual_store

        # Migrated to the dialect-backed store (ADR-002 step 2.3). The daemon
        # installs a RoutedStore over the same implementation; this is the
        # standalone-CLI fallback, so both paths now run one implementation.
        _default_store = sqlite_visual_store(state_db_path())
    return _default_store


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _unlink_all(paths: list[str]) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def _row_to_rec(r) -> VisualRecord:
    return VisualRecord(
        visual_id=r["visual_id"],
        thread_id=r["thread_id"],
        kind=r["kind"],
        title=r["title"],
        mime=r["mime"],
        path=r["path"],
        source=r["source"],
        created_ts=r["created_ts"],
    )
