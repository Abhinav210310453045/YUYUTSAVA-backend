"""SqliteUsageStore + UsageRecorder middleware (Phase 4 cost tracking).

Run:  uv run python -m unittest test.daemon.test_usage -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from yuyutsava.daemon.usage import (
    SqliteUsageStore,
    UsageRecorder,
    UsageRow,
    mint_usage_id,
)


def _row(
    *, task_id: str = "tsk_a", model: str = "m", ts: float = 1_000.0,
    input_tokens: int = 100, output_tokens: int = 10, cost: float = 0.5,
    role: str = "orchestrator",
) -> UsageRow:
    return UsageRow(
        id=mint_usage_id(), ts=ts, thread_id="th", task_id=task_id,
        role=role, model=model, input_tokens=input_tokens,
        output_tokens=output_tokens, est_cost_usd=cost,
    )


class SqliteUsageStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteUsageStore(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_add_and_list_roundtrip(self) -> None:
        row = _row()
        await self.store.add(row)
        rows = await self.store.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], row)

    async def test_list_filters(self) -> None:
        await self.store.add(_row(task_id="tsk_a", ts=100.0))
        await self.store.add(_row(task_id="tsk_b", ts=200.0))
        self.assertEqual(len(await self.store.list(task_id="tsk_a")), 1)
        self.assertEqual(len(await self.store.list(since=150.0)), 1)

    async def test_aggregate_totals_and_groups(self) -> None:
        # Known counts: 2 calls on model X for task a, 1 call on Y for b.
        await self.store.add(_row(task_id="tsk_a", model="X",
                                  input_tokens=100, output_tokens=10, cost=0.10))
        await self.store.add(_row(task_id="tsk_a", model="X",
                                  input_tokens=200, output_tokens=20, cost=0.20))
        await self.store.add(_row(task_id="tsk_b", model="Y",
                                  input_tokens=50, output_tokens=5, cost=0.05))

        total = await self.store.aggregate()
        self.assertEqual(len(total), 1)
        self.assertEqual(total[0].key, "all")
        self.assertEqual(total[0].calls, 3)
        self.assertEqual(total[0].input_tokens, 350)
        self.assertEqual(total[0].output_tokens, 35)
        self.assertAlmostEqual(total[0].est_cost_usd, 0.35)

        by_task = {a.key: a for a in await self.store.aggregate(group_by="task")}
        self.assertEqual(by_task["tsk_a"].calls, 2)
        self.assertAlmostEqual(by_task["tsk_a"].est_cost_usd, 0.30)
        self.assertEqual(by_task["tsk_b"].input_tokens, 50)
        # Most expensive group first.
        ordered = await self.store.aggregate(group_by="task")
        self.assertEqual(ordered[0].key, "tsk_a")

        by_model = {a.key: a for a in await self.store.aggregate(group_by="model")}
        self.assertEqual(set(by_model), {"X", "Y"})
        self.assertEqual(by_model["X"].input_tokens, 300)

    async def test_aggregate_by_day_and_since(self) -> None:
        await self.store.add(_row(ts=0.0))                  # 1970-01-01
        await self.store.add(_row(ts=86_400.0 + 60))        # 1970-01-02
        by_day = {a.key: a for a in await self.store.aggregate(group_by="day")}
        self.assertEqual(set(by_day), {"1970-01-01", "1970-01-02"})
        since = await self.store.aggregate(since=86_400.0, group_by="day")
        self.assertEqual([a.key for a in since], ["1970-01-02"])

    async def test_unknown_group_by_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.store.aggregate(group_by="user")  # type: ignore[arg-type]


class _FailingStore:
    async def add(self, row) -> None:
        raise RuntimeError("db down")


class UsageRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteUsageStore(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _recorder(self, store=None) -> UsageRecorder:
        return UsageRecorder(
            store if store is not None else self.store,
            role="orchestrator", model_name="test-model",
            task_id="tsk_1", thread_id="orch-1",
            prices={"test-model": (1.00, 5.00)},
        )

    @staticmethod
    def _state(usage: dict | None) -> dict:
        if usage is not None:
            usage = {"total_tokens": usage.get("input_tokens", 0)
                     + usage.get("output_tokens", 0), **usage}
        ai = AIMessage(content="hi", usage_metadata=usage)
        return {"messages": [HumanMessage(content="q"), ai]}

    async def test_records_row_with_cost(self) -> None:
        state = self._state(
            {"input_tokens": 200_000, "output_tokens": 40_000, "total_tokens": 240_000}
        )
        result = await self._recorder().aafter_model(state, None)
        self.assertIsNone(result)  # passive middleware: never edits state

        rows = await self.store.list()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(
            (r.task_id, r.thread_id, r.role, r.model),
            ("tsk_1", "orch-1", "orchestrator", "test-model"),
        )
        self.assertEqual((r.input_tokens, r.output_tokens), (200_000, 40_000))
        # 200k*$1/1M + 40k*$5/1M = 0.2 + 0.2
        self.assertAlmostEqual(r.est_cost_usd, 0.4)

    async def test_one_row_per_call(self) -> None:
        rec = self._recorder()
        await rec.aafter_model(self._state({"input_tokens": 10, "output_tokens": 1}), None)
        await rec.aafter_model(self._state({"input_tokens": 20, "output_tokens": 2}), None)
        rows = await self.store.list()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r.input_tokens for r in rows), 30)

    async def test_no_usage_metadata_skipped(self) -> None:
        await self._recorder().aafter_model(self._state(None), None)
        self.assertEqual(await self.store.list(), [])

    async def test_no_messages_skipped(self) -> None:
        await self._recorder().aafter_model({"messages": []}, None)
        self.assertEqual(await self.store.list(), [])

    async def test_store_failure_is_swallowed(self) -> None:
        rec = self._recorder(store=_FailingStore())
        # Must not raise — accounting never breaks a run.
        await rec.aafter_model(
            self._state({"input_tokens": 1, "output_tokens": 1}), None
        )


if __name__ == "__main__":
    unittest.main()
