"""Refreshing the price cache never destroys a price file it cannot read.

R-BUG-02. ``refresh_price_cache`` read ``model_prices.json`` through a helper
that returned ``{}`` for *any* failure, then wrote back only the model it had
just priced. One stray character in a hand-edited file, and the next start-up
replaced every hand-authored price with a single entry. A missing file and an
unreadable one are different answers; only the first is safe to write over.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yuyutsava.llm import pricing

SETTINGS = SimpleNamespace(model="some-model")


class PriceCache(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.prices = self.dir / pricing.PRICE_FILE
        self._live = mock.patch.object(pricing, "resolve_model_price", return_value=(1.5, 6.0))
        self._live.start()

    def tearDown(self) -> None:
        self._live.stop()
        self._tmp.cleanup()

    def _refresh(self):
        return pricing.refresh_price_cache(SETTINGS, dir_path=self.dir)

    def test_a_missing_file_is_created(self) -> None:
        self.assertEqual(self._refresh(), (1.5, 6.0))
        self.assertEqual(json.loads(self.prices.read_text()), {"some-model": [1.5, 6.0]})

    def test_hand_authored_entries_are_kept(self) -> None:
        self.prices.write_text(json.dumps({"my-model": [9.0, 9.0]}))
        self._refresh()
        self.assertEqual(
            json.loads(self.prices.read_text()),
            {"my-model": [9.0, 9.0], "some-model": [1.5, 6.0]},
        )

    def test_an_unreadable_file_is_left_exactly_as_it_was(self) -> None:
        broken = '{"my-model": [9.0, 9.0],, "other": [1, 2]}'
        self.prices.write_text(broken)
        with self.assertLogs("yuyutsava.llm.pricing", level="WARNING") as logs:
            self.assertIsNone(self._refresh())
        self.assertEqual(self.prices.read_text(), broken)
        self.assertIn(str(self.prices), "\n".join(logs.output))

    def test_a_file_that_is_not_an_object_is_left_too(self) -> None:
        self.prices.write_text("[1, 2, 3]")
        with self.assertLogs("yuyutsava.llm.pricing", level="WARNING"):
            self._refresh()
        self.assertEqual(self.prices.read_text(), "[1, 2, 3]")


if __name__ == "__main__":
    unittest.main()
