"""GET /usage: grouped aggregates over the llm_usage table.

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


def _row(*, task_id: str, model: str, tokens: int, cost: float) -> UsageRow:
    return UsageRow(
        id=mint_usage_id(), ts=1_000.0, thread_id="th", task_id=task_id,
        role="orchestrator", model=model, input_tokens=tokens,
        output_tokens=tokens // 10, est_cost_usd=cost,
    )


class UsageApiTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
