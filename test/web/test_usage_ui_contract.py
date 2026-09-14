"""Every field the app's usage panels read actually exists on the wire.

The renderer is JSX reaching into nested payloads (``usage.call.n``,
``row.priced``, ``summary.by_day[].key``). A renamed field does not fail a
build or a Python test — it renders as ``undefined`` in a panel nobody is
looking at yet, which is the worst possible failure for a number someone is
meant to trust.

So the key paths below are transcribed from the components, and this asserts
the backend really produces them:

* ``components/chat/ContextAside.jsx`` reads the ``usage`` websocket frame
  (``ContextSnapshot.as_dict()``);
* ``components/settings/UsageSettings.jsx`` reads ``GET /usage/summary`` and
  ``GET /usage/sessions``.

If a field here is renamed, rename it in the component in the same commit.

Run:  .venv/bin/python test/web/test_usage_ui_contract.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from yuyutsava.context.meter import ContextSnapshot
from yuyutsava.daemon.usage import SqliteUsageStore, UsageRow, mint_usage_id
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.services.stream_service import WebHub

# --- what the components read ---------------------------------------------

#: ContextAside.jsx + ContextMeter
FRAME_PATHS = (
    "model", "max_input_tokens", "used_tokens", "free_tokens", "used_fraction",
    "compact_trigger_tokens", "calibrated", "offloaded_digests",
    "call.n", "call.input_tokens", "call.output_tokens",
    "call.cache_read_tokens", "call.cache_hit_fraction", "call.est_cost_usd",
    "call.priced",
    "session.calls", "session.input_tokens", "session.output_tokens",
    "session.cache_read_tokens", "session.est_cost_usd",
    "session.compactions", "session.offloads",
)

#: UsageSettings.jsx — summary
SUMMARY_PATHS = (
    "totals.calls", "totals.input_tokens", "totals.output_tokens",
    "totals.est_cost_usd", "totals.cache_read_tokens", "unpriced_models",
)
SUMMARY_ROW_PATHS = ("key", "calls", "input_tokens", "output_tokens")

#: UsageSettings.jsx — sessions
SESSION_ROW_PATHS = (
    "thread_id", "title", "origin", "calls", "input_tokens",
    "cache_read_tokens", "output_tokens", "est_cost_usd", "priced", "models",
)


def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise AssertionError(f"missing {path!r} (stopped at {part!r})")
        cur = cur[part]
    return cur


class _Store:
    async def put_event_payload(self, **kw) -> None: ...
    async def put_proposal(self, p) -> None: ...
    async def put_decision(self, **kw) -> None: ...


class LiveFrameContract(unittest.TestCase):
    def test_the_websocket_frame_has_every_field_the_aside_reads(self):
        frame = {"type": "usage", **ContextSnapshot(
            thread_id="t1", model="gemini-3.5-flash", max_input_tokens=1_000_000,
            system_tokens=4_000, messages_tokens=29_200, call_no=12,
            input_tokens=41_087, cache_read_tokens=38_000, calls=12,
        ).as_dict()}
        for path in FRAME_PATHS:
            _dig(frame, path)

    def test_segments_carry_a_key_a_label_and_a_count(self):
        # The aside maps over these and keys React elements by `key`.
        for seg in ContextSnapshot().as_dict()["segments"]:
            self.assertEqual(set(seg), {"key", "label", "tokens"})

    def test_the_fields_are_json_types_a_panel_can_format(self):
        frame = ContextSnapshot(max_input_tokens=1_000, messages_tokens=10).as_dict()
        self.assertIsInstance(frame["used_fraction"], float)
        self.assertIsInstance(frame["used_tokens"], int)
        self.assertIsInstance(frame["call"]["priced"], bool)
        self.assertIsInstance(frame["model"], str)


class EndpointContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteUsageStore(Path(self._tmp.name) / "state.db")
        app = create_app(
            WebHub(store=_Store()), host="127.0.0.1", usage_store=self.store,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        )
        # Two conversations, two models, two days — enough that every optional
        # branch in the components has data to render.
        for i, (thread, model, ts, cached) in enumerate([
            ("conv-a", "claude-sonnet-4-5", 1_700_000_000.0, 900),
            ("conv-a", "gemini-3.5-flash", 1_700_000_100.0, 0),
            ("conv-b", "claude-sonnet-4-5", 1_700_090_000.0, 50),
        ]):
            await self.store.add(UsageRow(
                id=mint_usage_id(), ts=ts, thread_id=thread, task_id="",
                role="cli", model=model, input_tokens=1_000 * (i + 1),
                output_tokens=100, est_cost_usd=0.01,
                cache_read_tokens=cached,
            ))

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self._tmp.cleanup()

    async def test_summary_has_every_field_the_settings_section_reads(self):
        body = (await self.client.get("/usage/summary")).json()
        for path in SUMMARY_PATHS:
            _dig(body, path)
        self.assertTrue(body["by_model"], "no per-model rows to render")
        self.assertTrue(body["by_day"], "no per-day rows to render")
        for row in (*body["by_model"], *body["by_day"]):
            for path in SUMMARY_ROW_PATHS:
                _dig(row, path)

    async def test_the_day_series_has_more_than_one_point_to_plot(self):
        # The sparkline renders only with 2+ points; seeding across two days
        # is what makes that path covered rather than silently skipped.
        body = (await self.client.get("/usage/summary")).json()
        self.assertGreaterEqual(len(body["by_day"]), 2)

    async def test_sessions_have_every_field_the_table_reads(self):
        body = (await self.client.get("/usage/sessions")).json()
        self.assertTrue(body["rows"])
        for row in body["rows"]:
            for path in SESSION_ROW_PATHS:
                _dig(row, path)
            self.assertIsInstance(row["models"], list)
            self.assertIsInstance(row["priced"], bool)

    async def test_the_cached_column_can_be_computed_without_dividing_by_zero(self):
        # The table shows cache share as cache_read/input; a zero-input row
        # would render NaN.
        for row in (await self.client.get("/usage/sessions")).json()["rows"]:
            self.assertGreater(row["input_tokens"], 0)

    async def test_an_unpriced_model_is_reported_in_both_places(self):
        body = (await self.client.get("/usage/summary")).json()
        self.assertIn("gemini-3.5-flash", body["unpriced_models"])
        rows = (await self.client.get("/usage/sessions")).json()["rows"]
        mixed = next(r for r in rows if r["thread_id"] == "conv-a")
        self.assertFalse(mixed["priced"], "a mixed session must not claim a price")


if __name__ == "__main__":
    unittest.main(verbosity=2)
