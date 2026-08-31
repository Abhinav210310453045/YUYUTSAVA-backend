"""Transcript store: the full, durable conversation history per thread.

Where :mod:`yuyutsava.context.summary_store` keeps only the *condensed*
summary of a thread and LangGraph checkpoints keep the live state (swept after
~1h), this store keeps **every message verbatim** — one row per message, like
Cursor's per-bubble SQLite rows or Claude Code's per-session JSONL records.
:class:`~yuyutsava.context.transcript_policy.TranscriptRecorderPolicy`
appends messages here as the agent runs, so the transcript survives checkpoint
sweeps, daemon restarts, and compaction (which removes messages from live
state but never from this table).

This module now owns only the *contract*: :class:`TranscriptMessage`,
:class:`TranscriptStore` and the ``_encode`` wire format. The implementation is
:class:`~yuyutsava.context.transcript_store_unified.UnifiedTranscriptStore` —
one store over the dialect adapter, serving both a ``transcript_messages`` table
in ``state.db`` (zero-config) and the Postgres table from
:mod:`yuyutsava.storage.pg.migrations` (v7).

Each message is serialized with ``langchain_core.messages.message_to_dict``
so the typed record (role, content blocks, tool calls, tool_call_id) is
preserved with full fidelity. Dedup is by ``message_id`` (``INSERT OR IGNORE``
/ ``ON CONFLICT DO NOTHING``), so re-recording a resumed thread is idempotent.

Retention: transcripts are durable history, but ``delete_older_than`` exists
for parity with the other stores and lets operators bound growth via
:class:`yuyutsava.storage.sweeper.UnifiedSweeper` if desired.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import BaseMessage, message_to_dict

from yuyutsava.core.text_utils import sanitize_message_metadata

from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.context.transcript_store")

DEFAULT_LIST_LIMIT = 1_000


@dataclass(frozen=True)
class TranscriptMessage:
    """One persisted conversation message."""

    thread_id: str
    message_id: str
    seq: int
    type: str
    content: dict
    created_ts: float


def _encode(message: BaseMessage) -> tuple[str, str, str] | None:
    """Return ``(message_id, type, content_json)`` or ``None`` if unrecordable.

    Messages without a stable id are skipped — they get an id once the
    ``add_messages`` reducer folds them into state, so a later turn records
    them. Serializing via ``message_to_dict`` keeps the typed record intact.
    """
    message_id = getattr(message, "id", None)
    if not message_id:
        return None
    # Backstop for the streamed-metadata accretion bug (OpenRouter echoes
    # finish_reason/model_name per chunk; LangChain's chunk merge concatenates
    # them). Streaming already sanitizes, but persist a clean record even for
    # messages that reach the store via other paths. No-op on clean values.
    sanitize_message_metadata(message)
    return str(message_id), message.type, json.dumps(message_to_dict(message))


class TranscriptStore(ABC):
    """Interface both backends implement."""

    @abstractmethod
    async def put_messages(
        self,
        thread_id: str,
        messages: Sequence[BaseMessage],
        *,
        task_id: str | None = None,
    ) -> int:
        """Append messages not already stored (dedup on ``message_id``).

        Returns the number of rows actually inserted.
        """

    @abstractmethod
    async def list_messages(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[TranscriptMessage]:
        """Messages for ``thread_id`` ordered by ``seq`` ascending."""

    @abstractmethod
    async def delete_older_than(self, cutoff_ts: float) -> int:
        """TTL sweep hook (parity with the other stores). Returns rows deleted."""



# NOTE: SqliteTranscriptStore was replaced on 2026-08-08 by UnifiedTranscriptStore in
# context/transcript_store_unified.py (ADR-002 step 2.5b) — one implementation
# over the dialect adapter. Parity verified against both twins on both live
# backends in test/storage/test_transcript_store_parity.py.


# NOTE: PgTranscriptStore was replaced on 2026-08-08 by UnifiedTranscriptStore in
# context/transcript_store_unified.py (ADR-002 step 2.5b) — one implementation
# over the dialect adapter. Parity verified against both twins on both live
# backends in test/storage/test_transcript_store_parity.py.
