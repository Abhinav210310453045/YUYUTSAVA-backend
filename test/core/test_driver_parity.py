"""One driver, two surfaces: ``astream_agent`` still behaves as it did.

Phase 4 step 4.5, addressing [`F-D03`](../../docs/architecture-review/03-findings-dry-kiss.md#f-d03)
and ADR-004 item 5 — *"collapse ``astream_agent`` / ``astream_agent_iter`` into
one driver plus a sink"*.

The two were ~226 lines each and ~90% identical: build a config, loop over
``agent.astream(..., stream_mode=["messages", "updates"])``, collect messages,
detect interrupts, ask, resume. They differed only in what they did with each
event — one yields ``StreamEvent``s, the other prints to stderr and logs.

**They had already drifted.** The multi-interrupt resume protocol is implemented
in both, and only the daemon copy handles the case where several interrupts
arrive with no ids: the CLI copy builds ``Command(resume={})`` and silently
drops every answer the user gave. ``ResumeProtocolDrift`` below demonstrates
both halves. That is what "the resume protocol is hand-implemented twice" costs
in practice, and it is the argument for this step.

## What this suite pins

A scripted fake graph replays a fixed event stream, so no model is called and
nothing is billable. For each scenario the driver's **observable surface** is
captured — stderr, log records, the return value, and the ``Command`` objects
handed back to the graph — and compared against ``driver_golden.json``, recorded
from the pre-merge implementation.

Run:  .venv/bin/python test/core/test_driver_parity.py
"""

from __future__ import annotations

import io
import json
import logging
import pathlib
import re
import sys
import unittest
from contextlib import redirect_stderr
from typing import Any
from unittest.mock import patch

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

GOLDEN_PATH = pathlib.Path(__file__).resolve().parent / "driver_golden.json"


class _Interrupt:
    def __init__(self, value: Any, id: str | None = None) -> None:
        self.value = value
        self.id = id


class ScriptedGraph:
    """A ``CompiledStateGraph`` stand-in that replays fixed passes.

    Each pass is a list of ``(mode, data)`` tuples, exactly the shape
    ``astream(stream_mode=["messages", "updates"])`` yields. ``resumed`` records
    the ``Command`` the driver used to re-enter after each interrupt, which is
    where the resume-protocol divergence shows up.
    """

    def __init__(self, passes: list[list[tuple[str, Any]]]) -> None:
        self._passes = passes
        self._pass = 0
        self.resumed: list[Any] = []
        self.inputs: list[Any] = []

    async def astream(self, inp: Any, config: Any = None, stream_mode: Any = None):
        self.inputs.append(inp)
        if inp is not None and type(inp).__name__ == "Command":
            self.resumed.append(getattr(inp, "resume", None))
        events = self._passes[min(self._pass, len(self._passes) - 1)]
        self._pass += 1
        for ev in events:
            yield ev

    async def aget_state(self, config: Any):  # pragma: no cover - resume path
        class _S:
            next = ()
            values: dict = {}

        return _S()


def _ai(text: str, *, tool_calls: list[dict] | None = None,
        usage: dict | None = None) -> AIMessage:
    msg = AIMessage(content=text, id="ai-1", tool_calls=tool_calls or [])
    if usage:
        msg.usage_metadata = usage  # type: ignore[attr-defined]
    return msg


def _updates(*messages: Any) -> tuple[str, Any]:
    return ("updates", {"agent": {"messages": list(messages)}})


def _tokens(*texts: str) -> list[tuple[str, Any]]:
    return [("messages", (AIMessageChunk(content=t), {"langgraph_node": "agent"}))
            for t in texts]


def _interrupt(*ivs: _Interrupt) -> tuple[str, Any]:
    return ("updates", {"__interrupt__": list(ivs)})


#: label -> (passes, answers the user gives)
SCENARIOS: dict[str, tuple[list[list[tuple[str, Any]]], list[str]]] = {
    "plain reply": (
        [[*_tokens("Hello", " world"), _updates(_ai("Hello world"))]], []),
    "tool call then reply": (
        [[
            *_tokens("Let me look"),
            _updates(_ai("", tool_calls=[{"name": "tr_grep",
                                          "args": {"pattern": "x"}, "id": "c1"}])),
            _updates(ToolMessage(content="3 matches", tool_call_id="c1", name="tr_grep")),
            _updates(_ai("Found 3.")),
        ]], []),
    "tool error": (
        [[
            _updates(ToolMessage(content="Error: boom", tool_call_id="c1",
                                 name="tr_grep", status="error")),
            _updates(_ai("That failed.")),
        ]], []),
    "oversized tool result": (
        [[
            _updates(ToolMessage(content="z" * 2000, tool_call_id="c1", name="tr_grep")),
            _updates(_ai("Done.")),
        ]], []),
    "usage metadata": (
        [[_updates(_ai("Hi", usage={"input_tokens": 10, "output_tokens": 5,
                                     "total_tokens": 15}))]], []),
    "no output at all": ([[]], []),
    "one interrupt approved": (
        [
            [_interrupt(_Interrupt({"type": "permission_request"}, id="i1"))],
            [_updates(_ai("Ran it."))],
        ], ["approve"]),
    "one interrupt with no id": (
        [
            [_interrupt(_Interrupt({"type": "permission_request"}, id=None))],
            [_updates(_ai("Ran it."))],
        ], ["approve"]),
    "two interrupts with ids": (
        [
            [_interrupt(_Interrupt({"a": 1}, id="i1"), _Interrupt({"b": 2}, id="i2"))],
            [_updates(_ai("Both done."))],
        ], ["approve", "reject"]),
    "two interrupts with NO ids": (
        [
            [_interrupt(_Interrupt({"a": 1}, id=None), _Interrupt({"b": 2}, id=None))],
            [_updates(_ai("Both done."))],
        ], ["approve", "approve"]),
    # Added after a negative control found the matrix could not see them: two
    # deliberate breakages — never closing the AI stream on an interrupt, and
    # never firing on_tick — both went undetected because no scenario streamed
    # tokens straight into an interrupt, and none passed an on_tick hook.
    "tokens then interrupt": (
        [
            [*_tokens("Working"), _interrupt(_Interrupt({"a": 1}, id="i1"))],
            [_updates(_ai("Done."))],
        ], ["approve"]),
    # The tool-call-chunk guard closes the AI stream mid-flight. With only a
    # trailing update after it the newline lands either way, so a first version
    # of this scenario could not see the guard being removed at all — the text
    # chunk AFTER it is what makes the difference observable.
    "tool call chunk mid-stream": (
        [[
            *_tokens("thinking"),
            ("messages", (AIMessageChunk(
                content="calling", tool_call_chunks=[
                    {"name": "tr_grep", "args": "{}", "id": "c1", "index": 0}]),
                {"langgraph_node": "agent"})),
            *_tokens("and more"),
            _updates(_ai("Done.")),
        ]], []),
    "multi-pass ticks": (
        [
            [_updates(_ai("one")), _updates(_ai("two")),
             _interrupt(_Interrupt({"a": 1}, id="i1"))],
            [_updates(_ai("three"))],
        ], ["approve"]),
}

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _scrub(text: str) -> str:
    """Strip colour codes and the per-run thread id so output is comparable."""
    return _UUID.sub("<uuid>", _ANSI.sub("", text))


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(f"{record.levelname}:{_scrub(record.getMessage())}")


async def run_cli_driver(passes: list[list[tuple[str, Any]]],
                         answers: list[str]) -> dict[str, Any]:
    """Drive ``astream_agent`` over a scripted graph, capturing everything."""
    from yuyutsava.core import streaming

    graph = ScriptedGraph(passes)
    queue = list(answers)
    ticks: list[int] = []

    async def _fake_prompt(value: Any, **_kw: Any) -> str:
        return queue.pop(0) if queue else "reject"

    async def _on_tick(steps: int) -> None:
        ticks.append(steps)

    # ``streaming`` logs under the "yuyutsava" logger, and at the default level
    # its INFO records — every TOOL CALL / TOOL RESULT line — are filtered out
    # entirely. Capturing at DEBUG with propagation off separates the two
    # surfaces cleanly: `stderr` is what `print()` wrote, `logs` is what was
    # logged, and neither leaks into the other.
    logger = logging.getLogger("yuyutsava")
    handler = _Capture()
    previous_level, logger.level = logger.level, logging.DEBUG
    previous_propagate, logger.propagate = logger.propagate, False
    logger.addHandler(handler)
    buf = io.StringIO()
    try:
        with patch.object(streaming, "prompt_permission", _fake_prompt), \
                redirect_stderr(buf):
            final = await streaming.astream_agent(
                graph, "do the thing", thread_id="thr-fixed", agent_path="cli",
                on_tick=_on_tick)
    finally:
        logger.removeHandler(handler)
        logger.level = previous_level
        logger.propagate = previous_propagate

    return {
        "final": final,
        "stderr": _scrub(buf.getvalue()),
        "logs": handler.records,
        "resumed": [str(r) for r in graph.resumed],
        # The session runner coalesces store.touch off this; a driver that
        # stopped firing it would look fine everywhere else.
        "ticks": ticks,
    }


#: The one scenario the merge deliberately changes. Everything else is
#: compared byte for byte against `driver_golden.json`.
FIXED_BY_THE_MERGE = {"two interrupts with NO ids"}


class Parity(unittest.IsolatedAsyncioTestCase):
    """Every scenario matches what the pre-merge driver produced.

    Nine of ten are byte-identical — stderr, log records, return value and the
    resume ``Command``. The tenth is the drift the merge exists to fix.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.golden = (json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
                      if GOLDEN_PATH.exists() else {})

    async def test_every_scenario_matches(self) -> None:
        self.assertTrue(self.golden, "no golden record — capture it first")
        for label, (passes, answers) in SCENARIOS.items():
            if label in FIXED_BY_THE_MERGE:
                continue
            with self.subTest(scenario=label):
                got = await run_cli_driver(passes, answers)
                self.assertEqual(
                    got, self.golden[label],
                    f"{label}: the CLI driver's observable output changed",
                )

    async def test_the_only_change_is_the_one_intended(self) -> None:
        """Nothing but ``resumed`` moved, and only in the fixed scenario."""
        for label in FIXED_BY_THE_MERGE:
            passes, answers = SCENARIOS[label]
            got = await run_cli_driver(passes, answers)
            old = self.golden[label]
            with self.subTest(scenario=label):
                self.assertEqual(
                    {k: v for k, v in got.items() if k != "resumed"},
                    {k: v for k, v in old.items() if k != "resumed"},
                    f"{label}: something other than the resume command changed",
                )
                self.assertEqual(
                    old["resumed"], ["{}"],
                    "the golden no longer shows the old broken behaviour",
                )
                self.assertEqual(
                    got["resumed"], ["approve"],
                    "the CLI driver still drops multi-interrupt answers",
                )

    def test_every_scenario_has_a_recorded_outcome(self) -> None:
        """Negative control — an unrecorded scenario asserts nothing."""
        self.assertEqual([s for s in SCENARIOS if s not in self.golden], [])

    def test_the_scenarios_exercise_both_surfaces(self) -> None:
        """Negative control — output-free scenarios would make this vacuous."""
        self.assertTrue(any(g["stderr"] for g in self.golden.values()))
        self.assertTrue(any(g["logs"] for g in self.golden.values()))
        self.assertTrue(any(g["resumed"] for g in self.golden.values()))
        self.assertTrue(any(g["ticks"] for g in self.golden.values()))


class TheAgentProtocol(unittest.TestCase):
    """The driver declares what it needs instead of naming LangGraph's class.

    ``ScriptedGraph`` satisfying it is the whole point: every scenario above
    runs the real driver against something that is not a graph.
    """

    def test_the_real_graph_satisfies_it(self) -> None:
        from langgraph.graph.state import CompiledStateGraph

        from yuyutsava.ports import Agent

        self.assertTrue(issubclass(CompiledStateGraph, Agent))

    def test_the_scripted_double_satisfies_it(self) -> None:
        from yuyutsava.ports import Agent

        self.assertIsInstance(ScriptedGraph([]), Agent)

    def test_the_driver_no_longer_names_the_framework_class(self) -> None:
        src = (pathlib.Path(__file__).resolve().parents[2]
               / "yuyutsava/core/streaming.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "CompiledStateGraph", src,
            "streaming.py names LangGraph's compiled-graph class again; the "
            "driver should declare `Agent` and let the graph satisfy it",
        )

    def test_the_constructing_half_of_F_T03_is_still_open(self) -> None:
        """Honest scope: this closes the driving half only.

        ``create_deep_agent`` is still the only way this system builds an agent.
        Recording that here so the protocol above is not mistaken for a bigger
        claim than it is.
        """
        root = pathlib.Path(__file__).resolve().parents[2] / "yuyutsava"
        callers = [
            str(p.relative_to(root)) for p in root.rglob("*.py")
            if "__pycache__" not in p.parts
            and "create_deep_agent(" in p.read_text(encoding="utf-8")
        ]
        self.assertTrue(
            callers,
            "create_deep_agent has no callers — if the constructing half was "
            "addressed, ADR-004 item 4 can be marked done",
        )


class ResumeProtocolDrift(unittest.IsolatedAsyncioTestCase):
    """The bug that justifies merging the two drivers.

    LangGraph needs ``Command(resume={id: answer})`` when more than one
    interrupt is pending. When none of them carries an id, that map cannot be
    built — and the daemon driver falls back to a scalar resume with the first
    answer, with a comment explaining why. The CLI driver builds an empty map
    instead, which discards every answer the user just gave and resumes the
    graph with nothing.

    Two implementations of one protocol; the fix landed in one of them.
    """

    async def test_the_daemon_driver_keeps_the_answers(self) -> None:
        from yuyutsava.core.streaming import astream_agent_iter

        passes, answers = SCENARIOS["two interrupts with NO ids"]
        graph = ScriptedGraph(passes)
        queue = list(answers)

        async def ask(_value: Any) -> str:
            return queue.pop(0) if queue else "reject"

        [_ev async for _ev in astream_agent_iter(
            graph, "do the thing", thread_id="t", ask_handler=ask)]
        self.assertEqual(
            graph.resumed, ["approve"],
            "the daemon driver dropped the answers too",
        )

    async def test_the_cli_driver_used_to_drop_them(self) -> None:
        """Recorded from the pre-merge implementation, and now fixed."""
        golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            golden["two interrupts with NO ids"]["resumed"], ["{}"],
            "the golden no longer records the old behaviour, so the fix has "
            "nothing to be measured against",
        )

    async def test_both_drivers_now_agree(self) -> None:
        """The point of one shared ``_resume_command``."""
        from yuyutsava.core.streaming import astream_agent_iter

        passes, answers = SCENARIOS["two interrupts with NO ids"]

        daemon_graph = ScriptedGraph(passes)
        queue = list(answers)

        async def ask(_value: Any) -> str:
            return queue.pop(0) if queue else "reject"

        [_ev async for _ev in astream_agent_iter(
            daemon_graph, "do the thing", thread_id="t", ask_handler=ask)]
        cli = await run_cli_driver(passes, answers)
        self.assertEqual([str(r) for r in daemon_graph.resumed], cli["resumed"])


if __name__ == "__main__":
    if "--capture" in sys.argv:
        import asyncio

        async def _capture() -> None:
            out = {}
            for label, (passes, answers) in SCENARIOS.items():
                out[label] = await run_cli_driver(passes, answers)
            GOLDEN_PATH.write_text(
                json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"captured {len(out)} scenarios -> {GOLDEN_PATH}")

        asyncio.run(_capture())
    else:
        unittest.main(verbosity=2)
