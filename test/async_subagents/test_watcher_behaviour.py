"""Characterization: what the health watcher does with what the host reports.

`R-SOL-01`: the watcher is 724 lines and thirteen jobs — discovery, ingestion,
polling, progress, interrupts, resume, healing, timeouts, terminal verdicts,
error recovery and completion. Only discovery and the completion sink were
tested, so a split had almost nothing to be checked against.

What is pinned here is what a split would plausibly break: which host calls each
situation makes, which events come out in which order, and the several places
that must happen exactly once — an interrupt asked once, a thread healed once, a
completion notified once.

Driven entirely against :class:`~test.async_subagents.fake_host.FakeHost`.
"""

from __future__ import annotations

import unittest
from typing import Any

from test.async_subagents.fake_host import FakeHost
from yuyutsava.async_subagents.launch_index import LaunchIndex
from yuyutsava.async_subagents.mirror import AsyncTaskMirror
from yuyutsava.async_subagents.watcher import AsyncTaskHealthWatcher


class WatcherCase(unittest.IsolatedAsyncioTestCase):
    """One watcher over one fake host, with everything it emits recorded."""

    #: Replies the ask handler hands back, in order.
    replies: list[str] = ["approve"]

    async def asyncSetUp(self) -> None:
        self.host = FakeHost()
        self.mirror = AsyncTaskMirror()
        self.index = LaunchIndex()
        self.index.record("th-1", "T-parent", "cli")
        self.events: list[Any] = []
        self.asks: list[Any] = []
        self.completions: list[tuple] = []
        self._replies = list(self.replies)
        self.ask_raises = False

        async def ask_handler(ask):
            self.asks.append(ask)
            if self.ask_raises:
                raise RuntimeError("the surface went away")
            return self._replies.pop(0) if self._replies else "reject"

        async def event_sink(ev):
            self.events.append(ev)

        async def completion_sink(task, ok, summary):
            self.completions.append((task, ok, summary))

        self.watcher = AsyncTaskHealthWatcher(
            mirror=self.mirror,
            host_url="http://127.0.0.1:0",
            ask_handler=ask_handler,
            event_sink=event_sink,
            completion_sink=completion_sink,
            launch_index=self.index,
            per_task_timeout_sec=3600.0,
        )
        self.watcher._client = self.host

    async def ingest(self):
        """Discover the thread and return the task the mirror now holds."""
        await self.watcher._discover_new_threads()
        return next(iter(self.mirror.list_non_terminal()), None)

    def payloads(self) -> list[str]:
        """The class names of everything emitted, in order."""
        return [type(e.payload).__name__ for e in self.events]


class Discovery(WatcherCase):
    async def test_a_running_thread_is_mirrored_with_its_origin(self) -> None:
        task = await self.ingest()
        self.assertIsNotNone(task)
        self.assertEqual(task.parent_thread_id, "T-parent")
        self.assertEqual(task.origin, "cli")
        self.assertEqual(task.agent_name, "file-organizer")

    async def test_a_thread_is_only_ingested_once(self) -> None:
        await self.ingest()
        before = self.host.called("runs.list")
        await self.watcher._discover_new_threads()
        self.assertEqual(self.host.called("runs.list"), before, "no second ingest")

    async def test_a_host_that_cannot_be_listed_is_not_fatal(self) -> None:
        """The loop must survive a host hiccup and try again next cycle."""
        self.host.fail_on = "threads.search"
        await self.watcher._discover_new_threads()
        self.assertEqual(list(self.mirror.list_non_terminal()), [])


class Interrupts(WatcherCase):
    async def test_a_fresh_interrupt_is_asked_and_then_resumed(self) -> None:
        await self.ingest()
        self.host.interrupt_with({"question": "may I?"})
        await self.watcher._poll_known_tasks()

        self.assertEqual(len(self.asks), 1, "the user is asked exactly once")
        self.assertEqual(len(self.host.resumed), 1, "and the run is resumed once")
        self.assertEqual(
            self.host.resumed[0]["command"],
            {"resume": {"int-0": "approve"}},
            "resume is keyed by interrupt id",
        )
        self.assertIn("AsyncTaskAwaitingUserPayload", self.payloads())

    async def test_the_same_interrupt_is_never_asked_twice(self) -> None:
        """Resume is not instant, so the same interrupt is seen again next
        cycle; re-asking it would put a second prompt in front of the user."""
        await self.ingest()
        self.host.interrupt_with({"question": "may I?"})
        await self.watcher._poll_known_tasks()
        await self.watcher._poll_known_tasks()
        self.assertEqual(len(self.asks), 1)

    async def test_several_pending_interrupts_resume_in_one_call(self) -> None:
        """LangGraph refuses a bare resume value when more than one interrupt
        is pending — it has to be a map keyed by id."""
        self.replies = ["approve", "reject"]
        await self.asyncSetUp()
        await self.ingest()
        self.host.interrupt_with({"q": "first"}, {"q": "second"})
        await self.watcher._poll_known_tasks()
        self.assertEqual(len(self.asks), 2)
        self.assertEqual(len(self.host.resumed), 1)
        self.assertEqual(
            self.host.resumed[0]["command"],
            {"resume": {"int-0": "approve", "int-1": "reject"}},
        )

    async def test_an_ask_surface_that_raises_is_a_refusal(self) -> None:
        """A prompt that cannot be delivered is never consent."""
        await self.ingest()
        self.ask_raises = True
        self.host.interrupt_with({"q": "may I?"})
        await self.watcher._poll_known_tasks()
        self.assertEqual(self.host.resumed[0]["command"], {"resume": {"int-0": "reject"}})

    async def test_interrupted_with_nothing_pending_just_records_it(self) -> None:
        """Status says interrupted but every interrupt is already answered."""
        await self.ingest()
        self.host.run_status = "success"
        self.host.thread_status = "interrupted"
        self.host.interrupts = {}
        await self.watcher._poll_known_tasks()
        self.assertEqual(self.asks, [])
        self.assertEqual(self.host.resumed, [])
        self.assertEqual(self.mirror.list_non_terminal()[0].status, "interrupted")

    async def test_a_failed_resume_completes_the_task_as_an_error(self) -> None:
        """A task whose resume failed is finished, not left awaiting a user
        who has already answered."""
        await self.ingest()
        self.host.interrupt_with({"q": "may I?"})
        self.host.fail_on = "runs.create"
        await self.watcher._poll_known_tasks()
        self.assertEqual(len(self.completions), 1)
        _task, ok, summary = self.completions[0]
        self.assertFalse(ok)
        self.assertEqual(summary, "resume_failed")
        self.assertIn("AsyncTaskCompletedPayload", self.payloads())


class Timeouts(WatcherCase):
    async def test_a_task_past_its_deadline_is_cancelled_and_completed(self) -> None:
        task = await self.ingest()
        # MirroredTask is frozen, so move the deadline rather than the task.
        self.watcher._task_timeout = -1.0
        await self.watcher._poll_known_tasks()
        self.assertEqual(self.host.cancelled, ["r1"], "the run is cancelled on the host")
        self.assertEqual(self.mirror.get(task.task_id).status, "timeout")
        self.assertEqual(len(self.completions), 1)
        self.assertFalse(self.completions[0][1])

    async def test_a_host_that_cannot_cancel_still_completes_the_task(self) -> None:
        """Otherwise a task nobody can cancel is polled forever."""
        task = await self.ingest()
        self.watcher._task_timeout = -1.0
        self.host.fail_on = "runs.cancel"
        await self.watcher._poll_known_tasks()
        self.assertEqual(self.mirror.get(task.task_id).status, "timeout")
        self.assertEqual(len(self.completions), 1)


class TerminalVerdicts(WatcherCase):
    async def test_success_summarises_the_last_message(self) -> None:
        await self.ingest()
        self.host.run_status = "success"
        await self.watcher._poll_known_tasks()
        _task, ok, summary = self.completions[0]
        self.assertTrue(ok)
        self.assertIn("done the thing", summary)

    async def test_a_completion_is_notified_once(self) -> None:
        await self.ingest()
        self.host.run_status = "success"
        await self.watcher._poll_known_tasks()
        await self.watcher._poll_known_tasks()
        self.assertEqual(len(self.completions), 1)

    async def test_an_error_with_no_detail_anywhere_says_so(self) -> None:
        """Saying nothing would surface an unrelated success payload as the
        error; the launching agent then acts on a lie."""
        await self.ingest()
        self.host.run_status = "error"
        self.host.messages = []
        await self.watcher._poll_known_tasks()
        _task, ok, summary = self.completions[0]
        self.assertFalse(ok)
        self.assertIn("no error detail", summary)

    async def test_the_full_run_record_is_consulted_for_the_error(self) -> None:
        """runs.list usually omits the detail; runs.get usually carries it."""
        await self.ingest()
        self.host.run_status = "error"
        self.host.full_run_error = "the tool exploded"
        await self.watcher._poll_known_tasks()
        self.assertIn("the tool exploded", self.completions[0][2])


class SelfHealing(WatcherCase):
    #: An errored thread carrying an AIMessage(tool_calls) with no matching
    #: ToolMessage is unreadable — every later operation on it fails.
    ORPHANED = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "tc-1", "name": "tr_read", "args": {}}],
        }
    ]

    async def test_an_errored_thread_with_an_orphan_is_patched(self) -> None:
        await self.ingest()
        self.host.run_status = "error"
        self.host.messages = list(self.ORPHANED)
        await self.watcher._poll_known_tasks()
        self.assertEqual(len(self.host.patched), 1)


class Progress(WatcherCase):
    async def test_only_new_steps_are_emitted(self) -> None:
        await self.ingest()
        await self.watcher._poll_known_tasks()
        first = self.payloads().count("AsyncTaskProgressPayload")
        self.assertGreaterEqual(first, 1, "the step so far is reported")
        await self.watcher._poll_known_tasks()
        self.assertEqual(
            self.payloads().count("AsyncTaskProgressPayload"),
            first,
            "an unchanged thread re-reports nothing",
        )

    async def test_a_new_step_is_reported(self) -> None:
        await self.ingest()
        await self.watcher._poll_known_tasks()
        before = self.payloads().count("AsyncTaskProgressPayload")
        self.host.messages = [*self.host.messages, {"role": "assistant", "content": "and again"}]
        await self.watcher._poll_known_tasks()
        self.assertGreater(self.payloads().count("AsyncTaskProgressPayload"), before)


if __name__ == "__main__":
    unittest.main()
