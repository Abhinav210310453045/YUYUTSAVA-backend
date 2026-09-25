"""A background completion waits for its conversation's running turn (R-BUG-15).

When a background task finished, the orchestrator was woken on the thread of
the conversation that launched it — immediately, even while that conversation's
own turn was still streaming. Two graphs then wrote one checkpoint thread at
once; Gemini rejected the interleaved history ("Requests ending with a model
turn are not supported"), so the user was never told the task finished. The
wake-up now takes the conversation's turn gate: it waits for the running turn,
and holds the thread while it writes.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from yuyutsava.daemon.channels import ChannelRouter
from yuyutsava.daemon.orchestrator_loop import LoopServices, OrchestratorLoop
from yuyutsava.daemon.triage_loop import OrchestratorTask
from yuyutsava.daemon.turn_registry import TurnRegistry

THREAD = "chat-1"


def _wakeup() -> OrchestratorTask:
    return OrchestratorTask(
        proposal_id="",
        event_id="e1",
        topic="subagent.completed",
        summary="bg done",
        instruction="report back",
        subagent_hint="",
        urgency=1,
        kind="subagent_completed",
        parent_thread_id=THREAD,
    )


class TheGate(unittest.IsolatedAsyncioTestCase):
    async def test_hold_waits_for_the_running_turn_then_refuses_new_ones(self) -> None:
        turns, release = TurnRegistry(), asyncio.Event()

        async def body(run) -> None:
            await release.wait()

        self.assertIsNotNone(turns.start(thread_id=THREAD, body=body))
        entered = asyncio.Event()

        async def holder() -> None:
            async with turns.hold(THREAD):
                entered.set()
                self.assertIsNone(turns.start(thread_id=THREAD, body=body))
                await asyncio.sleep(0)

        task = asyncio.create_task(holder())
        await asyncio.sleep(0.05)
        self.assertFalse(entered.is_set(), "the hold did not wait for the running turn")
        release.set()
        await asyncio.wait_for(task, 2)
        self.assertTrue(entered.is_set())
        # Released: a turn can start again.
        self.assertIsNotNone(turns.start(thread_id=THREAD, body=body))
        release.set()


class TheWakeUpQueuesBehindTheTurn(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.turns = TurnRegistry()
        self.loop = OrchestratorLoop(
            task_queue=None,
            channels=ChannelRouter(channels=[]),
            store=SimpleNamespace(),
            orchestrator_model=object(),
            deps=SimpleNamespace(skill_registry=None, async_task_mirror=None, memory_store=None),
            orchestrator_token_budget=1000,
            services=LoopServices(turns=self.turns),
        )
        self.executed: list[tuple[str, bool]] = []

        async def fake_execute(task, *, thread_id, **_kw) -> None:
            # Recorded with whether a conversation turn was running meanwhile.
            self.executed.append((thread_id, self.turns.active(thread_id) is not None))

        self.loop._execute = fake_execute  # type: ignore[method-assign]

    async def test_it_runs_only_after_the_turn_ends(self) -> None:
        release = asyncio.Event()

        async def body(run) -> None:
            await release.wait()

        self.turns.start(thread_id=THREAD, body=body)
        wake = asyncio.create_task(self.loop._run_task(_wakeup()))
        await asyncio.sleep(0.05)
        self.assertEqual(self.executed, [], "the wake-up wrote while the turn was running")
        release.set()
        await asyncio.wait_for(wake, 2)
        self.assertEqual(self.executed, [(THREAD, False)])

    async def test_with_no_turn_running_it_runs_at_once(self) -> None:
        await asyncio.wait_for(self.loop._run_task(_wakeup()), 2)
        self.assertEqual(self.executed, [(THREAD, False)])


if __name__ == "__main__":
    unittest.main()
