"""``CheckAsyncTaskGuardPolicy`` guards polling exactly as the middleware did.

Phase 4 step 4.4, fourth migration — the one that uses the ``Raw`` escape hatch.

What is at stake: without this guard, a failed background task can be re-polled
until the graph hits its recursion limit and the whole turn crashes, with a full
traceback injected into context each time. Three properties carry that:

* a terminal status is **remembered**, and later checks are answered from the
  cache without hitting the server;
* the cached answer carries the "do not re-check" note — the model reads that,
  and it is the part that actually stops the loop;
* an ``error`` payload is **compacted** before it reaches context.

Both implementations are driven over the same sequences, including the stateful
one that matters most: check → terminal → check again.


## The golden record

The middleware was deleted at cutover, so it can no longer be run side by side.
Deleting it would have destroyed the evidence too, so its behaviour was
**captured first** into ``tool_policies_golden.json``. That file is the old
implementation's testimony; regenerating it is not a way to fix a failure, since
the code it came from no longer exists. A mismatch means the policy changed.

Run:  .venv/bin/python test/policy/test_check_guard_parity.py
"""

from __future__ import annotations

import json
import pathlib
import unittest
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from yuyutsava.async_subagents.check_guard_policy import CheckAsyncTaskGuardPolicy
from yuyutsava.policy.adapter import LangChainPolicyAdapter

CHECK = "check_async_task"


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _Request:
    def __init__(self, tool_name: str | None, task_id: str = "t1",
                 call_id: str = "c1") -> None:
        self.tool = _Tool(tool_name) if tool_name is not None else None
        self.tool_call = {
            "name": tool_name or "unknown",
            "args": {"task_id": task_id},
            "id": call_id,
        }
        self.state: dict = {}
        self.runtime = None


def _command(status: str, *, error: str | None = None,
             call_id: str = "c1") -> Command:
    payload: dict[str, Any] = {"status": status, "task_id": "t1"}
    if error is not None:
        payload["error"] = error
    return Command(update={
        "messages": [ToolMessage(json.dumps(payload), tool_call_id=call_id)],
    })


def _shape(result: Any) -> Any:
    """Comparable form: the payload dict a Command carries, or the raw value."""
    if not isinstance(result, Command):
        return {"kind": type(result).__name__, "value": repr(result)}
    update = result.update if isinstance(result.update, dict) else {}
    msgs = update.get("messages") or []
    if not msgs:
        return {"kind": "Command", "messages": []}
    tm = msgs[0]
    content = getattr(tm, "content", None)
    try:
        body = json.loads(content) if isinstance(content, str) else content
    except (ValueError, TypeError):
        body = content
    return {
        "kind": "Command",
        "body": body,
        "tool_call_id": getattr(tm, "tool_call_id", None),
    }


class _Driver:
    """Runs a sequence of calls against one implementation, tracking server hits."""

    def __init__(self, impl: Any) -> None:
        self.impl = impl
        self.server_hits = 0

    async def call(self, request: Any, returns: Any) -> Any:
        async def handler(_req: Any) -> Any:
            self.server_hits += 1
            return returns

        if isinstance(self.impl, CheckAsyncTaskGuardPolicy):
            adapter = LangChainPolicyAdapter([self.impl])
            return await adapter.awrap_tool_call(request, handler)
        return await self.impl.awrap_tool_call(request, handler)


GOLDEN = json.loads(
    (pathlib.Path(__file__).resolve().parent / "tool_policies_golden.json")
    .read_text(encoding="utf-8"))["check_guard"]


def _pair() -> tuple[_Driver]:
    """Only the policy now — the middleware was deleted; see the golden record."""
    return (_Driver(CheckAsyncTaskGuardPolicy()),)


class Parity(unittest.IsolatedAsyncioTestCase):
    async def test_a_running_check_passes_through(self) -> None:
        for driver in _pair():
            with self.subTest(impl=type(driver.impl).__name__):
                out = await driver.call(_Request(CHECK), _command("running"))
                self.assertEqual(_shape(out)["body"]["status"], "running")
                self.assertEqual(driver.server_hits, 1)

    async def test_a_terminal_result_is_cached_then_replayed(self) -> None:
        """The sequence that matters: the second check must not hit the server."""
        for driver in _pair():
            with self.subTest(impl=type(driver.impl).__name__):
                await driver.call(_Request(CHECK), _command("success"))
                replay = await driver.call(_Request(CHECK), _command("success"))
                self.assertEqual(
                    _shape(replay), GOLDEN["success"]["second"],
                    "the replayed answer differs from the middleware's",
                )
                self.assertEqual(
                    driver.server_hits, 1,
                    "a second check re-hit the server; the poll loop is not capped",
                )

    async def test_the_replay_carries_the_do_not_recheck_note(self) -> None:
        for driver in _pair():
            with self.subTest(impl=type(driver.impl).__name__):
                await driver.call(_Request(CHECK), _command("success"))
                replay = await driver.call(_Request(CHECK), _command("success"))
                note = _shape(replay)["body"].get("note", "")
                self.assertIn("Do not call check_async_task again", note)

    async def test_errors_are_compacted(self) -> None:
        traceback = "T" * 8000
        for driver in _pair():
            with self.subTest(impl=type(driver.impl).__name__):
                out = await driver.call(
                    _Request(CHECK), _command("error", error=traceback))
                body = _shape(out)["body"]
                self.assertEqual(
                    body, GOLDEN["error"]["first"]["body"],
                    "the compacted error differs from the middleware's",
                )
                self.assertLess(
                    len(body["error"]), len(traceback),
                    "the traceback was not compacted; it lands in context in full",
                )

    async def test_every_terminal_status_is_remembered(self) -> None:
        for status in ("success", "error", "cancelled", "timeout"):
            for driver in _pair():
                with self.subTest(status=status, impl=type(driver.impl).__name__):
                    await driver.call(_Request(CHECK), _command(status))
                    await driver.call(_Request(CHECK), _command(status))
                    self.assertEqual(
                        driver.server_hits, 1,
                        f"{status} was not treated as terminal",
                    )

    async def test_a_different_task_id_is_not_answered_from_the_cache(self) -> None:
        for driver in _pair():
            with self.subTest(impl=type(driver.impl).__name__):
                await driver.call(_Request(CHECK, task_id="t1"), _command("success"))
                await driver.call(_Request(CHECK, task_id="t2"), _command("running"))
                self.assertEqual(
                    driver.server_hits, 2,
                    "another task's check was answered from t1's cache",
                )

    async def test_a_non_check_tool_is_untouched(self) -> None:
        for driver in _pair():
            with self.subTest(impl=type(driver.impl).__name__):
                out = await driver.call(
                    _Request("start_async_task"), _command("success"))
                await driver.call(_Request("start_async_task"), _command("success"))
                self.assertEqual(driver.server_hits, 2)
                self.assertNotIn("note", _shape(out)["body"])

    async def test_an_unresolved_tool_is_untouched(self) -> None:
        """A hallucinated name must reach the framework, not this cache."""
        for driver in _pair():
            with self.subTest(impl=type(driver.impl).__name__):
                await driver.call(_Request(None), _command("success"))
                await driver.call(_Request(None), _command("success"))
                self.assertEqual(driver.server_hits, 2)

    async def test_a_missing_task_id_is_untouched(self) -> None:
        for driver in _pair():
            with self.subTest(impl=type(driver.impl).__name__):
                request = _Request(CHECK)
                request.tool_call["args"] = {}
                await driver.call(request, _command("success"))
                await driver.call(request, _command("success"))
                self.assertEqual(driver.server_hits, 2)

    async def test_a_non_command_result_passes_through(self) -> None:
        for driver in _pair():
            with self.subTest(impl=type(driver.impl).__name__):
                sentinel = ToolMessage("plain", tool_call_id="c1")
                out = await driver.call(_Request(CHECK), sentinel)
                self.assertIs(out, sentinel)

    async def test_unparseable_content_passes_through(self) -> None:
        for driver in _pair():
            with self.subTest(impl=type(driver.impl).__name__):
                broken = Command(update={
                    "messages": [ToolMessage("not json", tool_call_id="c1")]})
                out = await driver.call(_Request(CHECK), broken)
                self.assertIs(out, broken)
                await driver.call(_Request(CHECK), broken)
                self.assertEqual(
                    driver.server_hits, 2,
                    "an unparseable result was cached as terminal",
                )


class MatchesTheRecord(unittest.IsolatedAsyncioTestCase):
    """Every status, first call and replay, against what the middleware produced."""

    async def test_first_and_replay_match(self) -> None:
        for status, expected in GOLDEN.items():
            with self.subTest(status=status):
                d = _Driver(CheckAsyncTaskGuardPolicy())
                first = await d.call(
                    _Request(CHECK),
                    _command(status, error="T" * 8000 if status == "error" else None))
                second = await d.call(_Request(CHECK), _command(status))
                self.assertEqual(_shape(first), expected["first"], f"{status}: first call")
                self.assertEqual(_shape(second), expected["second"], f"{status}: replay")
                self.assertEqual(d.server_hits, expected["server_hits"], status)

    def test_the_record_covers_running_and_every_terminal_status(self) -> None:
        """Negative control — a record of only one status proves nothing."""
        self.assertEqual(
            set(GOLDEN),
            {"running", "success", "error", "cancelled", "timeout"})


class TheEscapeHatch(unittest.IsolatedAsyncioTestCase):
    """``Raw`` exists for this. Assert it is used, and used only here."""

    async def test_the_short_circuit_returns_raw(self) -> None:
        from yuyutsava.policy.types import Raw, ToolCall

        policy = CheckAsyncTaskGuardPolicy()
        call = ToolCall(name=CHECK, args={"task_id": "t1"}, id="c1",
                        resolved_tool=CHECK)
        self.assertIsNone(
            await policy.before_tool(call),
            "nothing is cached yet, so the first check must go through",
        )
        await policy.after_tool(call, _command("success"))
        decision = await policy.before_tool(call)
        self.assertIsInstance(
            decision, Raw,
            "the cached replay is a framework Command, which is exactly what "
            "Raw is for — Denied would claim the call was refused",
        )

    def test_raw_is_used_by_exactly_one_policy(self) -> None:
        """The hatch is a signal. A second unrelated user means a missing concept."""
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "yuyutsava"
        users: list[str] = []
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts or path.parts[-2:-1] == ("policy",):
                continue
            src = path.read_text(encoding="utf-8")
            if "policy.types import" not in src and "policy import" not in src:
                continue
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id == "Raw":
                    users.append(str(path.relative_to(root)))
                    break
        self.assertEqual(
            sorted(set(users)), ["async_subagents/check_guard_policy.py"],
            f"Raw is now used by {sorted(set(users))}. ADR-004: a second "
            f"unrelated use means the protocol is missing a concept — add the "
            f"concept, do not widen the hatch.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
