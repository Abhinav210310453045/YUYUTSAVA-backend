"""A restart must not execute what the user declined.

Observed on a live daemon. Two ``mode=triage`` submissions sat ``queued`` with
``proposal=pending`` / ``decision=expired`` — proposed to the user, never
answered, timed out. On the next boot ``resume_interrupted_tasks`` re-enqueued
them and the orchestrator **ran both**, one of them attempting to write to the
user's TODO board.

That contradicts the system's own second invariant: *no filesystem write,
delete, or shell command executes without either a standing rule or an explicit
user approval.* A restart was a way around Tier-1 consent.

## Why the status alone cannot decide it

``TaskRecord`` carries no ``proposal_id``, so at boot the two meanings of
``queued`` are indistinguishable:

* ``submit_direct`` writes an **approved** proposal and enqueues immediately —
  ``queued`` only for the instant between those two calls;
* ``submit_via_triage`` publishes and stops — ``queued`` for its whole life
  while a proposal waits, and still ``queued`` if it is skipped, dropped, or
  expires.

So ``queued`` is not resumed. The cost of being wrong in that direction is a
re-submission; the cost of the other direction is running something declined.

Run:  .venv/bin/python test/daemon/test_resume_consent_boundary.py
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from yuyutsava.daemon.orchestrator_loop import resume_interrupted_tasks


class _Rec:
    def __init__(self, task_id: str, status: str, *, thread_id: str | None = None,
                 instruction: str = "do the thing") -> None:
        self.task_id = task_id
        self.status = status
        self.thread_id = thread_id
        self.instruction = instruction
        self.origin = "api"
        self.complexity = None


class _Registry:
    """Stands in for TaskRegistry; records which statuses were listed."""

    def __init__(self, rows: dict[str, list[_Rec]]) -> None:
        self._rows = rows
        self.listed: list[str] = []

    async def list(self, *, status: str, limit: int = 200):
        self.listed.append(status)
        return list(self._rows.get(status, [])), None


def _registry() -> _Registry:
    return _Registry({
        "running": [_Rec("tsk_running", "running", thread_id="orch-abc")],
        "queued": [
            _Rec("tsk_expired", "queued", instruction="declined by timeout"),
            _Rec("tsk_pending", "queued", instruction="still awaiting the user"),
        ],
    })


async def _drain(q: asyncio.Queue) -> list[Any]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


class QueuedIsNotResumed(unittest.IsolatedAsyncioTestCase):
    async def test_a_queued_task_is_never_re_enqueued(self) -> None:
        q: asyncio.Queue = asyncio.Queue()
        n = await resume_interrupted_tasks(_registry(), q)
        ids = [t.task_id for t in await _drain(q)]
        self.assertNotIn(
            "tsk_expired", ids,
            "a task whose Tier-1 proposal expired was re-enqueued — a restart "
            "is bypassing consent",
        )
        self.assertNotIn("tsk_pending", ids)
        self.assertEqual(n, 1)

    async def test_a_running_task_still_resumes(self) -> None:
        """The durable-resume feature must survive the fix."""
        q: asyncio.Queue = asyncio.Queue()
        await resume_interrupted_tasks(_registry(), q)
        (task,) = await _drain(q)
        self.assertEqual(task.task_id, "tsk_running")
        self.assertEqual(
            task.resume_thread_id, "orch-abc",
            "the running task lost its thread_id, so it restarts from scratch "
            "instead of continuing from its checkpoint",
        )

    async def test_queued_rows_are_reported_not_silently_dropped(self) -> None:
        """Skipping them quietly would be its own silent failure."""
        q: asyncio.Queue = asyncio.Queue()
        with self.assertLogs("yuyutsava.daemon.orchestrator_loop", level="INFO") as cm:
            await resume_interrupted_tasks(_registry(), q)
        joined = "\n".join(cm.output)
        self.assertIn("NOT resumed", joined)
        self.assertIn("tsk_expired", joined)

    async def test_no_registry_is_a_no_op(self) -> None:
        q: asyncio.Queue = asyncio.Queue()
        self.assertEqual(await resume_interrupted_tasks(None, q), 0)
        self.assertTrue(q.empty())

    async def test_a_running_row_without_a_thread_id_still_runs(self) -> None:
        """It was authorised; it just has no checkpoint to continue from."""
        reg = _Registry({"running": [_Rec("tsk_nothread", "running")], "queued": []})
        q: asyncio.Queue = asyncio.Queue()
        self.assertEqual(await resume_interrupted_tasks(reg, q), 1)
        (task,) = await _drain(q)
        self.assertIsNone(task.resume_thread_id)


class TheListingIsScoped(unittest.IsolatedAsyncioTestCase):
    """Negative control — a fix that simply stopped listing would also pass above."""

    async def test_queued_is_still_read_so_it_can_be_reported(self) -> None:
        reg = _registry()
        await resume_interrupted_tasks(reg, asyncio.Queue())
        self.assertIn("running", reg.listed)
        self.assertIn(
            "queued", reg.listed,
            "queued rows are no longer read at all, so the operator gets no "
            "report of what was left behind",
        )

    async def test_a_listing_failure_does_not_break_boot(self) -> None:
        class _Broken(_Registry):
            async def list(self, *, status: str, limit: int = 200):
                raise RuntimeError("db down")

        q: asyncio.Queue = asyncio.Queue()
        self.assertEqual(await resume_interrupted_tasks(_Broken({}), q), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
