"""The usage endpoints: grouped aggregates, a range summary, per-session spend.

``GET /usage`` is the original grouped aggregate. ``/usage/summary`` and
``/usage/sessions`` back the app's Settings -> Usage section, and carry two
properties worth pinning: a per-day series is ordered for plotting rather than
by cost, and an unpriced model is reported as unpriced instead of letting a
total quietly read low.

Run:  uv run python -m unittest test.web.test_usage_api -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from yuyutsava.daemon.usage import SqliteUsageStore, UsageRow, mint_usage_id
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.services.stream_service import WebHub


class _RecordingStore:
    async def put_event_payload(self, **kw) -> None: ...
    async def put_proposal(self, p) -> None: ...
    async def put_decision(self, **kw) -> None: ...


def _row(*, task_id: str, model: str, tokens: int, cost: float,
         thread_id: str = "th", ts: float = 1_000.0, cached: int = 0) -> UsageRow:
    return UsageRow(
        id=mint_usage_id(), ts=ts, thread_id=thread_id, task_id=task_id,
        role="orchestrator", model=model, input_tokens=tokens,
        output_tokens=tokens // 10, est_cost_usd=cost, cache_read_tokens=cached,
    )


class _UsageApiBase(unittest.IsolatedAsyncioTestCase):
    """App + a real SQLite usage store. Shared, not inherited for reuse of
    tests — subclassing the aggregate case would re-run all of it three times."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.usage_store = SqliteUsageStore(Path(self._tmp.name) / "state.db")
        app = create_app(
            WebHub(store=_RecordingStore()), host="127.0.0.1",
            usage_store=self.usage_store,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self._tmp.cleanup()

    async def _seed(self) -> None:
        await self.usage_store.add(_row(task_id="tsk_a", model="X", tokens=100, cost=0.10))
        await self.usage_store.add(_row(task_id="tsk_a", model="X", tokens=200, cost=0.20))
        await self.usage_store.add(_row(task_id="tsk_b", model="Y", tokens=50, cost=0.05))


class UsageApiTests(_UsageApiBase):
    async def test_totals_when_ungrouped(self) -> None:
        await self._seed()
        r = await self.client.get("/usage")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsNone(body["group_by"])
        self.assertEqual(len(body["rows"]), 1)
        self.assertEqual(body["rows"][0]["key"], "all")
        self.assertEqual(body["rows"][0]["calls"], 3)
        self.assertEqual(body["rows"][0]["input_tokens"], 350)
        self.assertAlmostEqual(body["rows"][0]["est_cost_usd"], 0.35)

    async def test_group_by_model(self) -> None:
        await self._seed()
        r = await self.client.get("/usage", params={"group_by": "model"})
        self.assertEqual(r.status_code, 200)
        rows = {row["key"]: row for row in r.json()["rows"]}
        self.assertEqual(set(rows), {"X", "Y"})
        self.assertEqual(rows["X"]["calls"], 2)
        self.assertEqual(rows["X"]["input_tokens"], 300)
        self.assertAlmostEqual(rows["Y"]["est_cost_usd"], 0.05)

    async def test_group_by_task_ordered_by_cost(self) -> None:
        await self._seed()
        r = await self.client.get("/usage", params={"group_by": "task"})
        keys = [row["key"] for row in r.json()["rows"]]
        self.assertEqual(keys, ["tsk_a", "tsk_b"])  # most expensive first

    async def test_invalid_group_by_rejected(self) -> None:
        r = await self.client.get("/usage", params={"group_by": "user"})
        self.assertEqual(r.status_code, 422)

    async def test_missing_store_is_503(self) -> None:
        app = create_app(WebHub(store=_RecordingStore()), host="127.0.0.1")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            r = await client.get("/usage")
        self.assertEqual(r.status_code, 503)


class UsageSummaryTests(_UsageApiBase):
    async def test_it_returns_totals_per_model_and_a_day_series(self) -> None:
        await self._seed()
        r = await self.client.get("/usage/summary")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["totals"]["calls"], 3)
        self.assertEqual(body["totals"]["input_tokens"], 350)
        self.assertEqual({m["key"] for m in body["by_model"]}, {"X", "Y"})
        self.assertEqual(len(body["by_day"]), 1)

    async def test_the_day_series_is_ordered_for_plotting_not_by_cost(self) -> None:
        # Every store aggregate comes back most-expensive-first, which is the
        # wrong order for a time axis.
        await self.usage_store.add(_row(task_id="t", model="X", tokens=10,
                                        cost=0.01, ts=1_000.0))
        await self.usage_store.add(_row(task_id="t", model="X", tokens=999,
                                        cost=9.99, ts=1_000.0 + 86_400 * 3))
        body = (await self.client.get("/usage/summary")).json()
        keys = [d["key"] for d in body["by_day"]]
        self.assertEqual(keys, sorted(keys))
        self.assertGreater(len(keys), 1)

    async def test_an_unpriced_model_is_named(self) -> None:
        # "$0.00" in a total is indistinguishable from a free call; the client
        # needs to know the figure is an undercount.
        await self.usage_store.add(
            _row(task_id="t", model="not-a-real-model-xyz", tokens=10, cost=0.0))
        body = (await self.client.get("/usage/summary")).json()
        self.assertIn("not-a-real-model-xyz", body["unpriced_models"])

    async def test_an_empty_range_returns_a_zero_row_not_an_error(self) -> None:
        body = (await self.client.get("/usage/summary?since=9999999999")).json()
        self.assertEqual(body["totals"]["calls"], 0)
        self.assertEqual(body["by_model"], [])
        self.assertEqual(body["by_day"], [])


class UsageSessionsTests(_UsageApiBase):
    async def test_each_conversation_gets_one_row(self) -> None:
        await self.usage_store.add(_row(task_id="t", model="X", tokens=100,
                                        cost=0.1, thread_id="conv-a", cached=60))
        await self.usage_store.add(_row(task_id="t", model="Y", tokens=200,
                                        cost=0.2, thread_id="conv-a"))
        await self.usage_store.add(_row(task_id="t", model="X", tokens=50,
                                        cost=0.05, thread_id="conv-b"))
        r = await self.client.get("/usage/sessions")
        self.assertEqual(r.status_code, 200)
        rows = {row["thread_id"]: row for row in r.json()["rows"]}
        self.assertEqual(set(rows), {"conv-a", "conv-b"})
        self.assertEqual(rows["conv-a"]["calls"], 2)
        self.assertEqual(rows["conv-a"]["input_tokens"], 300)
        self.assertEqual(rows["conv-a"]["cache_read_tokens"], 60)
        self.assertEqual(sorted(rows["conv-a"]["models"]), ["X", "Y"])

    async def test_rows_are_ordered_most_recently_active_first(self) -> None:
        await self.usage_store.add(_row(task_id="t", model="X", tokens=1,
                                        cost=0.0, thread_id="old", ts=1_000.0))
        await self.usage_store.add(_row(task_id="t", model="X", tokens=1,
                                        cost=0.0, thread_id="new", ts=9_000.0))
        rows = (await self.client.get("/usage/sessions")).json()["rows"]
        self.assertEqual([r["thread_id"] for r in rows], ["new", "old"])

    async def test_a_conversation_with_an_unpriced_model_says_so(self) -> None:
        await self.usage_store.add(_row(task_id="t", model="no-such-model-abc",
                                        tokens=10, cost=0.0, thread_id="c"))
        row = (await self.client.get("/usage/sessions")).json()["rows"][0]
        self.assertFalse(row["priced"])

    async def test_calls_made_outside_any_conversation_are_reported_not_dropped(self) -> None:
        await self.usage_store.add(_row(task_id="t", model="X", tokens=7,
                                        cost=0.0, thread_id=""))
        rows = (await self.client.get("/usage/sessions")).json()["rows"]
        blank = [r for r in rows if r["thread_id"] == ""]
        self.assertEqual(len(blank), 1)
        self.assertEqual(blank[0]["title"], "unattributed")

    async def test_the_limit_is_bounded(self) -> None:
        self.assertEqual(
            (await self.client.get("/usage/sessions?limit=0")).status_code, 422)
        self.assertEqual(
            (await self.client.get("/usage/sessions?limit=501")).status_code, 422)

    async def test_an_empty_store_returns_no_rows(self) -> None:
        self.assertEqual((await self.client.get("/usage/sessions")).json()["rows"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
