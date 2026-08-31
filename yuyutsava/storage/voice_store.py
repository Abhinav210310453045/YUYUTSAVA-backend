"""Voice-message store: per-turn conversation rows for voice/chat threads.

Where :mod:`yuyutsava.context.transcript_store` keeps the *verbatim LangChain
message record* (every human/ai/tool message, for context fidelity), this store
keeps the **human-facing conversation surface of a voice session** — one row per
spoken turn, with the text *and* a reference to the synthesized TTS audio so the
UI can re-render and **replay** a resumed voice conversation (Phase 6b).

It is deliberately separate from ``transcript_messages``:

  * transcript rows carry tool calls, system messages, content blocks — noise
    for a chat bubble list and impossible to attach audio to cleanly;
  * voice rows are a thin, ordered ``(role, text, audio?)`` list keyed by
    ``(thread_id, seq)`` — exactly what the Voice panel renders.

Agent TTS audio is always persisted (it's what "▶ replay" plays back); the user
side stores the STT *transcript* (text) by default, with the raw user audio
optional/off (privacy + size). Audio bytes live on disk under
``blobs/voice/`` (see :func:`yuyutsava.storage.paths.blobs_dir`), mirroring the
``event_payloads`` blob convention; the DB row holds the path. Unlike scratch
blobs, these are **session-scoped user history** — deleted when the session is
deleted, not aged out by the TTL sweeper.

Two interchangeable backends behind :class:`VoiceMessageStore`, same shape as
:mod:`yuyutsava.context.transcript_store`:

  * :class:`SqliteVoiceMessageStore` — a ``voice_messages`` table in ``state.db``.
  * :class:`PgVoiceMessageStore` — the ``voice_messages`` table created by
    :mod:`yuyutsava.storage.pg.migrations` (v11).
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.storage.voice_store")

DEFAULT_LIST_LIMIT = 1_000

# Valid column values — kept loose (plain TEXT in the DB) but validated here so a
# typo surfaces at the call site rather than as a silently un-renderable row.
ROLES = ("user", "assistant")
MODALITIES = ("text", "audio")


@dataclass(frozen=True)
class VoiceMessage:
    """One persisted voice-conversation turn."""

    thread_id: str
    seq: int
    role: str          # "user" | "assistant"
    modality: str      # "text" | "audio"
    text: str
    audio_blob_path: str | None
    sample_rate: int | None
    created_ts: float

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_blob_path)


class VoiceMessageStore(ABC):
    """Interface both backends implement."""

    @abstractmethod
    async def put_message(
        self,
        thread_id: str,
        *,
        role: str,
        text: str,
        modality: str = "text",
        audio_blob_path: str | None = None,
        sample_rate: int | None = None,
    ) -> int:
        """Append one turn; returns its monotonically increasing ``seq``."""

    @abstractmethod
    async def list_messages(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[VoiceMessage]:
        """Turns for ``thread_id`` ordered by ``seq`` ascending."""

    @abstractmethod
    async def get_message(self, thread_id: str, seq: int) -> VoiceMessage | None:
        """One turn by ``(thread_id, seq)`` — used to serve its audio blob."""

    @abstractmethod
    async def delete_for_thread(self, thread_id: str) -> int:
        """Drop every row for a thread (on session delete). Returns rows deleted."""


def _validate(role: str, modality: str) -> None:
    if role not in ROLES:
        raise ValueError(f"voice message role must be one of {ROLES}, got {role!r}")
    if modality not in MODALITIES:
        raise ValueError(f"voice message modality must be one of {MODALITIES}, got {modality!r}")


# ---------------------------------------------------------------------------
# NOTE: SqliteVoiceMessageStore and PgVoiceMessageStore lived here until
# 2026-08-08, replaced by ``voice_store_unified.py`` (ADR-002 step 2.5b) — one
# implementation over the dialect adapter.
#
# Justified by test_voice_store_parity.py: 52 assertions run against both twins
# AND the unified store on both live backends before the twins were deleted.
#
# This module keeps the shared vocabulary: ROLES, MODALITIES, VoiceMessage,
# _validate, and the VoiceMessageStore interface.
# ---------------------------------------------------------------------------
