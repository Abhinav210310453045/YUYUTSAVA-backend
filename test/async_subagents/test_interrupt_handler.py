"""What the watcher does when a background run pauses on ``interrupt()``.

``InterruptHandler.handle`` reads the run's thread, asks the user about each
fresh interrupt once, then resumes the run once with every reply keyed by
interrupt id. An ask that raises is a refusal; a resume that fails finishes the
task instead of leaving it awaiting an answer already given. A fake Agent
Protocol client records what the handler asks of the host.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from typing import Any

from yuyutsava.async_subagents.mirror import AsyncTaskMirror, MirroredTask
from yuyutsava.async_subagents.watcher.context import WatcherContext
from yuyutsava.async_subagents.watcher.interrupts import InterruptHandler


class _Client:
    def __init__(self, thread: Any, *, fail_get: bool = False, fail_create: bool = False) -> None:
        self.thread, self.fail_get, self.fail_create = thread, fail_get, fail_create
        self.creates: list[dict] = []
        self.threads = self
        self.runs = _Runs(self)

    async def get(self, *, thread_id: str) -> Any:
        if self.fail_get:
            raise RuntimeError("host down")
        return self.thread


class _Runs:
    def __init__(self, client: _Client) -> None:
        self.client = client

    async def create(self, **kw: Any) -> None:
        if self.client.fail_create:
            raise RuntimeError("resume refused")
        self.client.creates.append(kw)


def _task() -> MirroredTask:
    now = time.time()
    return MirroredTask(
        task_id="t1",
        agent_name="general-purpose-bg",
        graph_id="general-purpose",
        instruction="x",
        status="running",
        started_at=now,
        last_update_at=now,
        parent_thread_id="chat-1",
        sub_thread_id="sub-1",
    )


def _thread(*interrupts: tuple[str, Any], as_dict: bool = True) -> Any:
    items = [{"id": i, "value": v} for i, v in interrupts]
    if as_dict:
        return {"interrupts": {"node": items}}

    class _T:
        pass

    t = _T()
    t.interrupts = {"node": [type("I", (), {"id": i, "value": v})() for i, v in interrupts]}
    return t


class HandlerCase(unittest.TestCase):
    def _run(self, client: _Client, *, replies: list[Any], pass_thread: bool = False):
        mirror = AsyncTaskMirror()
        asked, events, completed = [], [], []
        answers = iter(replies)

        async def ask(prompt):
            asked.append(prompt)
            answer = next(answers)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        async def emit(event):
            events.append(type(event.payload).__name__)

        async def sink(task, ok, summary):
            completed.append((ok, summary))

        ctx = WatcherContext(mirror=mirror, ask=ask, emit=emit, client=client, completion_sink=sink)
        handler = InterruptHandler(ctx)

        async def go():
            await mirror.upsert(_task())
            thread = client.thread if pass_thread else None
            await handler.handle(mirror.get("t1"), "run-1", thread)
            return mirror.get("t1")

        task = asyncio.run(asyncio.wait_for(go(), 10))
        return task, asked, events, completed, ctx


class AskedThenResumed(HandlerCase):
    def test_every_fresh_interrupt_is_asked_and_the_run_resumed_once_by_id(self) -> None:
        client = _Client(
            _thread(("i1", {"type": "permission_request", "command": "rm x"}), ("i2", "which?"))
        )
        task, asked, events, _, _ = self._run(client, replies=["approve", "blue"])
        self.assertEqual([a.interrupt_id for a in asked], ["i1", "i2"])
        self.assertEqual(asked[1].interrupt_value["type"], "user_question")
        self.assertEqual(asked[0].surface, "background")
        self.assertEqual(asked[0].agent_path, "orchestrator/general-purpose-bg#bg")
        (create,) = client.creates
        self.assertEqual(create["command"], {"resume": {"i1": "approve", "i2": "blue"}})
        self.assertEqual(
            (create["thread_id"], create["assistant_id"]), ("sub-1", "general-purpose")
        )
        self.assertEqual((task.status, task.pending_ask_id), ("running", None))
        self.assertEqual(
            events,
            [
                "AsyncTaskAwaitingUserPayload",
                "AsyncTaskAwaitingUserPayload",
                "AsyncTaskProgressPayload",
            ],
        )

    def test_attribute_style_threads_and_a_passed_thread_work_too(self) -> None:
        client = _Client(_thread(("i1", "q"), as_dict=False), fail_get=True)
        task, asked, *_ = self._run(client, replies=["yes"], pass_thread=True)
        self.assertEqual(len(asked), 1)
        self.assertEqual(task.status, "running")

    def test_an_interrupt_is_asked_once_across_cycles(self) -> None:
        client = _Client(_thread(("i1", "q")))
        _, _, _, _, ctx = self._run(client, replies=["a"])
        self.assertEqual(ctx.handled_interrupts["t1"], {"i1"})


class Refusals(HandlerCase):
    def test_an_ask_that_raises_is_a_rejection(self) -> None:
        client = _Client(_thread(("i1", "q")))
        self._run(client, replies=[RuntimeError("surface gone")])
        self.assertEqual(client.creates[0]["command"], {"resume": {"i1": "reject"}})

    def test_a_failed_resume_finishes_the_task(self) -> None:
        client = _Client(_thread(("i1", "q")), fail_create=True)
        task, _, events, completed, _ = self._run(client, replies=["ok"])
        self.assertEqual((task.status, task.error), ("error", "resume_failed"))
        self.assertIn("AsyncTaskCompletedPayload", events)
        self.assertEqual(completed, [(False, "resume_failed")])


class NothingToAsk(HandlerCase):
    def test_no_fresh_interrupts_marks_interrupted_and_waits(self) -> None:
        task, asked, _, _, _ = self._run(_Client({"interrupts": {}}), replies=[])
        self.assertEqual((task.status, asked), ("interrupted", []))

    def test_an_unreadable_thread_changes_nothing(self) -> None:
        task, asked, _, _, _ = self._run(_Client(None, fail_get=True), replies=[])
        self.assertEqual((task.status, asked), ("running", []))


if __name__ == "__main__":
    unittest.main()
