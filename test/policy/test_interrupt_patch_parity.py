"""``AsyncTaskInterruptPatchPolicy`` — same behaviour, minus one crash.

Phase 4 step 4.4, fifth migration, and **the one deliberate behaviour change** in
the policy migrations.

## The change

``AsyncTaskInterruptPatchMiddleware.awrap_tool_call`` opened with
``request.tool.name in _INTERRUPT_TOOLS``. ``request.tool`` is ``None`` whenever
the model names a tool that is not bound — a hallucination or a typo — so every
such call raised ``AttributeError: 'NoneType' object has no attribute 'name'``
and took down the whole turn, where the framework would otherwise have reported
an unknown tool and let the model recover.

``BackgroundTaskCapMiddleware``, its sibling, guarded that exact case and carried
a comment explaining why. This one did not. Finding BA.

``TheCrashThatWas`` asserts both halves: the middleware really raises, and the
policy really does not. It is written as a *demonstration*, not an assertion from
a docstring — if a future deepagents version starts resolving unknown tools, the
first case fails and this note needs revisiting.

Everything else is parity: which tools trigger a patch, which thread gets
patched, and every reason the lookup declines.


## The golden record

The middleware was deleted at cutover, so it can no longer be run side by side.
Deleting it would have destroyed the evidence too, so its behaviour was
**captured first** into ``tool_policies_golden.json``. That file is the old
implementation's testimony; regenerating it is not a way to fix a failure, since
the code it came from no longer exists. A mismatch means the policy changed.

Run:  .venv/bin/python test/policy/test_interrupt_patch_parity.py
"""

from __future__ import annotations

import json
import pathlib
import unittest
from typing import Any

from yuyutsava.async_subagents.interrupt_patch_policy import (
    AsyncTaskInterruptPatchPolicy,
)
from yuyutsava.policy.adapter import LangChainPolicyAdapter

SPECS = [{"name": "researcher", "url": "http://sub:2024", "headers": {}}]
TRACKED = {"async_tasks": {"t1": {"agent_name": "researcher", "thread_id": "th-1"}}}


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _Request:
    def __init__(self, tool_name: str | None, *, task_id: str = "t1",
                 state: dict | None = None) -> None:
        self.tool = _Tool(tool_name) if tool_name is not None else None
        self.tool_call = {
            "name": tool_name or "made_up_tool",
            "args": {"task_id": task_id},
            "id": "c1",
        }
        self.state = TRACKED if state is None else state
        self.runtime = None


class _Patched:
    """Records (agent, thread) pairs the implementation asked to patch."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, agent_name: str, thread_id: str) -> int:
        self.calls.append((agent_name, thread_id))
        return 1


GOLDEN = json.loads(
    (pathlib.Path(__file__).resolve().parent / "tool_policies_golden.json")
    .read_text(encoding="utf-8"))["interrupt_patch"]


async def _drive_new(request: Any, recorder: _Patched) -> tuple[Any, list]:
    policy = AsyncTaskInterruptPatchPolicy(SPECS)
    policy._patcher.patch_pending = recorder  # type: ignore[assignment]
    adapter = LangChainPolicyAdapter([policy])

    async def handler(_req: Any) -> str:
        return "RAN"

    return await adapter.awrap_tool_call(request, handler), recorder.calls


#: (label, tool name, task_id, state, expected patch targets)
CASES: list[tuple[str, str, str, dict | None, list]] = [
    ("update triggers a patch", "update_async_task", "t1", None,
     [("researcher", "th-1")]),
    ("cancel triggers a patch", "cancel_async_task", "t1", None,
     [("researcher", "th-1")]),
    ("an unrelated tool does not", "start_async_task", "t1", None, []),
    ("check does not", "check_async_task", "t1", None, []),
    ("no task_id", "cancel_async_task", "", None, []),
    ("task not tracked", "cancel_async_task", "t9", None, []),
    ("empty state", "cancel_async_task", "t1", {}, []),
    ("no async_tasks key", "cancel_async_task", "t1", {"messages": []}, []),
    ("record missing thread_id", "cancel_async_task", "t1",
     {"async_tasks": {"t1": {"agent_name": "researcher"}}}, []),
    ("record missing agent_name", "cancel_async_task", "t1",
     {"async_tasks": {"t1": {"thread_id": "th-1"}}}, []),
    ("unknown subagent", "cancel_async_task", "t1",
     {"async_tasks": {"t1": {"agent_name": "stranger", "thread_id": "th-1"}}}, []),
]


class Parity(unittest.IsolatedAsyncioTestCase):
    async def test_every_case_matches_the_middleware(self) -> None:
        for label, tool, task_id, state, expected in CASES:
            with self.subTest(case=label):
                result, calls = await _drive_new(
                    _Request(tool, task_id=task_id, state=state), _Patched())
                recorded = GOLDEN[label]
                self.assertEqual(
                    [list(c) for c in calls], recorded["patched"],
                    f"{label}: patch targets differ from the middleware's",
                )
                self.assertEqual(
                    [list(c) for c in calls], [list(c) for c in expected],
                    f"{label}: unexpected targets",
                )
                self.assertEqual(repr(result), recorded["result"], f"{label}: result")

    def test_every_case_has_a_recorded_outcome(self) -> None:
        """Negative control — a case the record does not cover asserts nothing."""
        self.assertEqual([l for l, _, _, _, _ in CASES if l not in GOLDEN], [])

    async def test_the_matrix_both_patches_and_declines(self) -> None:
        """Negative control — all-patch or all-decline would prove nothing."""
        outcomes = {bool(expected) for *_, expected in CASES}
        self.assertEqual(outcomes, {True, False})

    async def test_the_tool_always_runs(self) -> None:
        """This policy never refuses; the patch is a side effect."""
        for label, tool, task_id, state, _ in CASES:
            with self.subTest(case=label):
                result, _ = await _drive_new(
                    _Request(tool, task_id=task_id, state=state), _Patched())
                self.assertEqual(result, "RAN")


class TheCrashThatWas(unittest.IsolatedAsyncioTestCase):
    """A tool name the model invented. Finding BA."""

    def test_the_middleware_crashed_the_turn(self) -> None:
        """Recorded before deletion: the capture run raised on this input.

        ``__unresolved__`` is absent from the golden file for exactly that
        reason — the capture could not record an outcome, because there wasn't
        one. Its absence IS the evidence.
        """
        self.assertNotIn(
            "__unresolved__", GOLDEN,
            "an outcome was recorded for the unresolved-tool case, so the "
            "middleware did NOT crash — finding BA needs revisiting",
        )

    async def test_the_policy_lets_it_through(self) -> None:
        result, calls = await _drive_new(_Request(None), _Patched())
        self.assertEqual(
            result, "RAN",
            "an unresolved tool call no longer reaches the framework's "
            "unknown-tool handling",
        )
        self.assertEqual(calls, [], "a patch was attempted for a tool that is not bound")


class TheWholePoint(unittest.IsolatedAsyncioTestCase):
    """The lookup, with no adapter and no framework request object."""

    def _policy(self) -> AsyncTaskInterruptPatchPolicy:
        return AsyncTaskInterruptPatchPolicy(SPECS)

    def _call(self, tool: str | None, task_id: str = "t1", state: dict | None = None):
        from yuyutsava.policy.types import ToolCall

        return ToolCall(
            name=tool or "made_up_tool", args={"task_id": task_id}, id="c1",
            state=TRACKED if state is None else state, resolved_tool=tool)

    def test_it_finds_the_tracked_thread(self) -> None:
        self.assertEqual(
            self._policy()._tracked(self._call("cancel_async_task")),
            {"agent_name": "researcher", "thread_id": "th-1"},
        )

    def test_it_declines_a_subagent_this_master_did_not_launch(self) -> None:
        state = {"async_tasks": {"t1": {"agent_name": "stranger", "thread_id": "th"}}}
        self.assertIsNone(
            self._policy()._tracked(self._call("cancel_async_task", state=state)),
            "a thread belonging to another master would have been patched",
        )

    async def test_an_unresolved_tool_never_reaches_the_lookup(self) -> None:
        policy = self._policy()
        recorder = _Patched()
        policy._patcher.patch_pending = recorder  # type: ignore[assignment]
        self.assertIsNone(await policy.before_tool(self._call(None)))
        self.assertEqual(recorder.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
