"""A preference row that is not JSON is reported, not silently dropped.

The SQLite prefs table stores TEXT, so a hand-edited or half-written row can
hold anything. The store used to swallow any exception reading it — a corrupt
preference simply vanished. It still degrades (default / left out of the list)
but now says which key, and only a JSON error is treated that way.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from yuyutsava.storage.events.sqlite_backend import SqliteEventsBackend, SqlitePrefsBackend


class CorruptPreference(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.backend = SqliteEventsBackend(Path(self._tmp.name) / "state.db")
        await self.backend.open()
        self.prefs = SqlitePrefsBackend(self.backend)
        await self.prefs.put("good", {"a": 1})
        await self.backend.execute(
            "INSERT INTO user_prefs(key, value_json, updated_ts) VALUES(?,?,?)",
            ("broken", "{not json", time.time()),
        )

    async def asyncTearDown(self) -> None:
        await self.backend.close()
        self._tmp.cleanup()

    async def test_get_falls_back_to_the_default_and_names_the_key(self) -> None:
        with self.assertLogs("yuyutsava.storage.events.sqlite", level="WARNING") as logs:
            self.assertEqual(await self.prefs.get("broken", "dflt"), "dflt")
        self.assertIn("user_prefs[broken]", logs.output[0])

    async def test_list_leaves_it_out_and_names_the_key(self) -> None:
        with self.assertLogs("yuyutsava.storage.events.sqlite", level="WARNING") as logs:
            self.assertEqual(await self.prefs.list(), {"good": {"a": 1}})
        self.assertIn("user_prefs[broken]", logs.output[0])

    async def test_a_good_row_reads_back(self) -> None:
        self.assertEqual(await self.prefs.get("good"), {"a": 1})


if __name__ == "__main__":
    unittest.main()
