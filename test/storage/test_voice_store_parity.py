"""``UnifiedVoiceMessageStore`` matches both twins, on both backends.

Third domain migrated onto the dialect adapter (Phase 2 step 2.5b). Same
acceptance shape as visuals and summaries. It ran against all four
implementations — both twins and the unified store on each dialect, 52
assertions — and the twins were deleted once they agreed.

Two properties here are deliberately pinned rather than assumed, because the two
previous migrations each turned up a wrong assumption:

* ``test_concurrent_puts_get_distinct_seqs`` — ``seq`` is auto-increment on both
  backends, so unlike ``ThreadSummaryStore`` there is no ``MAX()+1`` race. That
  is a claim about the schema, so it gets a test.
* ``test_delete_does_not_touch_audio_blobs`` — the row holds
  ``audio_blob_path`` but does **not** own the file; ``audio_io.blobs`` does,
  and ``purge_session`` removes it separately. Copying the visuals pattern
  (which *does* unlink) would double-delete.

Run:  .venv/bin/python test/storage/test_voice_store_parity.py
"""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from yuyutsava.storage.backend import DEFAULT_PG_DSN


def _pg_dsn() -> str:
    return os.environ.get("YUYUTSAVA_PG_DSN", "").strip() or DEFAULT_PG_DSN


def _pg_reachable() -> bool:
    u = urlparse(_pg_dsn())
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 5432), timeout=1.5):
            return True
    except OSError:
        return False


PG_UP = _pg_reachable()


class _VoiceContract:
    """Behaviour every VoiceMessageStore implementation must satisfy."""

    async def test_put_then_get_roundtrip(self) -> None:
        seq = await self.store.put_message(
            self.thread, role="user", text="hello",
            modality="audio", audio_blob_path="/tmp/a.wav", sample_rate=16000,
        )
        got = await self.store.get_message(self.thread, seq)
        self.assertIsNotNone(got)
        self.assertEqual(got.seq, seq)
        self.assertEqual(got.thread_id, self.thread)
        self.assertEqual(got.role, "user")
        self.assertEqual(got.text, "hello")
        self.assertEqual(got.modality, "audio")
        self.assertEqual(got.audio_blob_path, "/tmp/a.wav")
        self.assertEqual(got.sample_rate, 16000)
        self.assertIsInstance(got.created_ts, float)
        self.assertGreater(got.created_ts, 0)

    async def test_seq_increases(self) -> None:
        a = await self.store.put_message(self.thread, role="user", text="1")
        b = await self.store.put_message(self.thread, role="assistant", text="2")
        self.assertGreater(b, a)

    async def test_get_unknown_is_none(self) -> None:
        self.assertIsNone(await self.store.get_message(self.thread, 99999))

    async def test_list_is_seq_ascending(self) -> None:
        for i in range(3):
            await self.store.put_message(self.thread, role="user", text=f"m{i}")
        rows = await self.store.list_messages(self.thread)
        self.assertEqual([r.text for r in rows], ["m0", "m1", "m2"])
        self.assertEqual([r.seq for r in rows], sorted(r.seq for r in rows))

    async def test_list_after_seq_filters(self) -> None:
        first = await self.store.put_message(self.thread, role="user", text="a")
        await self.store.put_message(self.thread, role="user", text="b")
        rows = await self.store.list_messages(self.thread, after_seq=first)
        self.assertEqual([r.text for r in rows], ["b"])

    async def test_list_respects_limit(self) -> None:
        for i in range(4):
            await self.store.put_message(self.thread, role="user", text=f"m{i}")
        self.assertEqual(len(await self.store.list_messages(self.thread, limit=2)), 2)

    async def test_threads_are_isolated(self) -> None:
        await self.store.put_message(self.thread, role="user", text="mine")
        await self.store.put_message(self.thread + "-other", role="user", text="theirs")
        rows = await self.store.list_messages(self.thread)
        self.assertEqual([r.text for r in rows], ["mine"])

    async def test_defaults_when_optional_fields_omitted(self) -> None:
        seq = await self.store.put_message(self.thread, role="user", text="")
        got = await self.store.get_message(self.thread, seq)
        self.assertEqual(got.text, "")
        self.assertEqual(got.modality, "text")
        self.assertIsNone(got.audio_blob_path)
        self.assertIsNone(got.sample_rate)

    async def test_delete_for_thread(self) -> None:
        await self.store.put_message(self.thread, role="user", text="a")
        await self.store.put_message(self.thread, role="user", text="b")
        await self.store.put_message(self.thread + "-other", role="user", text="keep")

        self.assertEqual(await self.store.delete_for_thread(self.thread), 2)
        self.assertEqual(await self.store.list_messages(self.thread), [])
        self.assertEqual(
            len(await self.store.list_messages(self.thread + "-other")), 1,
            "another thread's messages were deleted",
        )

    async def test_delete_unknown_thread_is_zero(self) -> None:
        self.assertEqual(await self.store.delete_for_thread("no-such-thread"), 0)

    async def test_rejects_invalid_role(self) -> None:
        with self.assertRaises(ValueError):
            await self.store.put_message(self.thread, role="wizard", text="x")

    async def test_concurrent_puts_get_distinct_seqs(self) -> None:
        """``seq`` is database-assigned, so concurrency cannot duplicate it.

        Explicitly tested because the summary-store migration found the opposite
        pattern (``MAX()+1``) racing on Postgres. Same-shaped API, different
        allocation mechanism, different safety — worth proving, not assuming.
        """
        results = await asyncio.gather(
            *(self.store.put_message(self.thread, role="user", text=f"m{i}")
              for i in range(5)),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(errors, [], f"concurrent puts raised: {errors}")
        self.assertEqual(len(set(results)), 5, f"duplicate seq allocated: {results}")

    async def test_delete_does_not_touch_audio_blobs(self) -> None:
        """The store holds the path; ``audio_io.blobs`` owns the file."""
        blob = Path(tempfile.mkdtemp()) / "clip.wav"
        blob.write_bytes(b"RIFF-fake-audio")
        await self.store.put_message(
            self.thread, role="user", text="x",
            modality="audio", audio_blob_path=str(blob), sample_rate=16000,
        )
        await self.store.delete_for_thread(self.thread)
        self.assertTrue(
            blob.exists(),
            "the store unlinked an audio blob it does not own — purge_session "
            "already deletes these, so this would be a double delete",
        )


class _SqliteCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "state.db"
        self.thread = "thread-voice-parity"

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()


class SqliteUnified(_VoiceContract, _SqliteCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from yuyutsava.storage.voice_store_unified import sqlite_voice_store

        self.store = sqlite_voice_store(self.db)


class _PgCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self.thread = f"thread-voice-{os.getpid()}-{id(self)}"
        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM voice_messages WHERE thread_id LIKE %s", (self.thread + "%",)
            )
        await self.pool.close()


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresUnified(_VoiceContract, _PgCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from yuyutsava.storage.voice_store_unified import pg_voice_store

        self.store = pg_voice_store(self.pool)


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
