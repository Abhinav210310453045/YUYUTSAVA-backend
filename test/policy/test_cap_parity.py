"""``BackgroundTaskCapPolicy`` refuses exactly what the middleware refused.

Phase 4 step 4.4, second migration.

Small policy, three things worth pinning:

**The gate is the resolved tool, not the requested name.** They differ only when
the model names a tool that is not bound, and there the cap must stay out of the
way so the framework's unknown-tool path runs. The middleware carried a comment
saying exactly that; its sibling ``AsyncTaskInterruptPatchMiddleware`` omitted
the same guard and crashed turns (finding BA). ``UnresolvedTool`` keeps that
guard honest on both sides.

**The boundary is ``>=``.** At the cap, refuse; one below, allow. Off-by-one here
either wedges the master at ``max-1`` or lets it exceed the cap.

**The refusal shape.** ``status="error"`` and no ``name`` label — different from
the permission refusal, deliberately carried over rather than harmonised.


## The golden record

The middleware was deleted at cutover, so it can no longer be run side by side.
Deleting it would have destroyed the evidence too, so its behaviour was
**captured first** into ``tool_policies_golden.json``. That file is the old
implementation's testimony; regenerating it is not a way to fix a failure, since
the code it came from no longer exists. A mismatch means the policy changed.

Run:  .venv/bin/python test/policy/test_cap_parity.py
"""

from __future__ import annotations

import json
import pathlib
import unittest
from typing import Any

from yuyutsava.async_subagents.cap_policy import BackgroundTaskCapPolicy
from yuyutsava.policy.adapter import LangChainPolicyAdapter

MAX = 3


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _Mirror:
    """Stands in for AsyncTaskMirror; only ``count_running`` is consulted."""

    def __init__(self, running: int) -> None:
        self._running = running
        self.calls = 0

    def count_running(self) -> int:
        self.calls += 1
        return self._running


class _Request:
    def __init__(self, tool_name: str | None, args: dict | None = None,
                 call_id: str = "c1", requested: str | None = None) -> None:
        self.tool = _Tool(tool_name) if tool_name is not None else None
        self.tool_call = {
            "name": requested if requested is not None else (tool_name or ""),
            "args": args or {},
            "id": call_id,
        }
        self.state: dict = {}
        self.runtime = None


async def _drive(policy_or_mw: Any, request: Any) -> tuple[Any, bool]:
    ran: list[bool] = []

    async def handler(_req: Any) -> str:
        ran.append(True)
        return "RAN"

    if isinstance(policy_or_mw, BackgroundTaskCapPolicy):
        adapter = LangChainPolicyAdapter([policy_or_mw])
        out = await adapter.awrap_tool_call(request, handler)
    else:
        out = await policy_or_mw.awrap_tool_call(request, handler)
    return out, bool(ran)


def _shape(result: Any, ran: bool) -> tuple[str, str, str, Any]:
    """(verdict, content, status, name) — comparable across both sides."""
    if ran:
        return ("allowed", "", "", None)
    return (
        "refused",
        str(getattr(result, "content", "")),
        str(getattr(result, "status", "")),
        getattr(result, "name", None),
    )


CASES: list[tuple[str, int, str | None]] = [
    # label, tasks already running, resolved tool name (None = unresolved)
    ("under the cap",        MAX - 1, "start_async_task"),
    ("at the cap",           MAX,     "start_async_task"),
    ("over the cap",         MAX + 5, "start_async_task"),
    ("empty mirror",         0,       "start_async_task"),
    ("other tool at cap",    MAX,     "check_async_task"),
    ("other tool under cap", 0,       "cancel_async_task"),
]


GOLDEN = json.loads(
    (pathlib.Path(__file__).resolve().parent / "tool_policies_golden.json")
    .read_text(encoding="utf-8"))["cap"]


class Parity(unittest.IsolatedAsyncioTestCase):
    async def test_every_case_matches_the_middleware(self) -> None:
        for label, running, tool in CASES:
            with self.subTest(case=label):
                new = await _drive(
                    BackgroundTaskCapPolicy(_Mirror(running), MAX),
                    _Request(tool))
                self.assertEqual(
                    list(_shape(*new)), GOLDEN[label],
                    f"{label}: the policy differs from what the middleware did",
                )

    def test_every_case_has_a_recorded_outcome(self) -> None:
        """Negative control — a case the record does not cover asserts nothing."""
        missing = [label for label, _, _ in CASES if label not in GOLDEN]
        self.assertEqual(missing, [])

    async def test_the_matrix_covers_both_verdicts(self) -> None:
        """Negative control — all-allow or all-refuse would make the above vacuous."""
        verdicts = set()
        for _, running, tool in CASES:
            result, ran = await _drive(
                BackgroundTaskCapPolicy(_Mirror(running), MAX), _Request(tool))
            verdicts.add(_shape(result, ran)[0])
        self.assertEqual(verdicts, {"allowed", "refused"})


class TheBoundary(unittest.IsolatedAsyncioTestCase):
    """``>=`` — off by one either wedges the master or busts the cap."""

    async def test_one_below_the_cap_is_allowed(self) -> None:
        _, ran = await _drive(
            BackgroundTaskCapPolicy(_Mirror(MAX - 1), MAX),
            _Request("start_async_task"))
        self.assertTrue(ran)

    async def test_exactly_at_the_cap_is_refused(self) -> None:
        _, ran = await _drive(
            BackgroundTaskCapPolicy(_Mirror(MAX), MAX),
            _Request("start_async_task"))
        self.assertFalse(ran)


class UnresolvedTool(unittest.IsolatedAsyncioTestCase):
    """A tool name the model invented must reach the framework's own handling.

    The requested name is deliberately ``start_async_task`` here — the tool the
    cap guards — while nothing resolved. A policy gating on the *requested* name
    would refuse it and hide a hallucination behind a plausible cap message.
    """

    async def test_the_policy_stays_out_of_the_way(self) -> None:
        _, ran = await _drive(
            BackgroundTaskCapPolicy(_Mirror(MAX + 10), MAX),
            _Request(None, requested="start_async_task"))
        self.assertTrue(
            ran, "an unresolved tool call was refused by the cap; the framework "
                 "never got to report the unknown tool")

    async def test_the_middleware_did_the_same(self) -> None:
        """From the record: the middleware also let an unresolved call through."""
        self.assertEqual(GOLDEN["__unresolved__"][0], "allowed")


class RefusalShape(unittest.IsolatedAsyncioTestCase):
    async def test_it_reports_as_an_error_and_is_unnamed(self) -> None:
        result, _ = await _drive(
            BackgroundTaskCapPolicy(_Mirror(MAX), MAX),
            _Request("start_async_task"))
        self.assertEqual(result.status, "error")
        self.assertIsNone(
            result.name,
            "the cap refusal gained a name label; the permission refusal has "
            "one and this one did not, and that difference is carried over "
            "deliberately rather than harmonised inside a migration",
        )

    async def test_it_names_the_numbers_the_model_needs(self) -> None:
        result, _ = await _drive(
            BackgroundTaskCapPolicy(_Mirror(MAX), MAX),
            _Request("start_async_task"))
        self.assertIn(f"{MAX}/{MAX}", result.content)
        self.assertIn("cancel_async_task", result.content)


class TheWholePoint(unittest.IsolatedAsyncioTestCase):
    """No framework, no adapter, no graph — just the decision."""

    async def test_the_policy_alone_refuses_at_the_cap(self) -> None:
        from yuyutsava.policy.types import Denied, ToolCall

        policy = BackgroundTaskCapPolicy(_Mirror(MAX), MAX)
        decision = await policy.before_tool(
            ToolCall(name="start_async_task", resolved_tool="start_async_task"))
        self.assertIsInstance(decision, Denied)

    async def test_the_policy_alone_allows_below_it(self) -> None:
        from yuyutsava.policy.types import ToolCall

        policy = BackgroundTaskCapPolicy(_Mirror(0), MAX)
        self.assertIsNone(await policy.before_tool(
            ToolCall(name="start_async_task", resolved_tool="start_async_task")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
