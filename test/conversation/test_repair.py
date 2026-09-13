"""A conversation cannot be left in a state the model refuses to continue from.

Reconstructed from a real wedged thread (`ui-1789323666-…`, 13 Sep). The user
sent a message while `tr_run_python` was running; LangGraph cancelled the tool
task and fabricated a `status="success"` result whose body said "cancelled".
Every call after that returned `finish_reason: STOP` with `output_tokens: 0`,
empty content and no tool calls — four messages, ~70,000 input tokens each, and
not one word back.

These pin the repair and the contract around it:

* the fabricated result is found and rewritten as an explicit error;
* the message **id is preserved**, because `add_messages` merges by id and a
  fresh id would append a second result for one call;
* a clean history is left completely alone;
* the wording differs by cause — "the user interjected, nothing ran" is not
  the same instruction as "consent was never given";
* the empty-output warnings are `notice`, not `log`, because a `log` is
  droppable and this one was dropped four times.

Run:  .venv/bin/python test/conversation/test_repair.py
"""

from __future__ import annotations

import asyncio
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import yuyutsava.core.config  # noqa: F401 — import first (core/__init__ cycle)
from yuyutsava.conversation.repair import (
    CANCELLED_TOOL_MARKER,
    Cause,
    needs_repair,
    repair_orphan_tool_calls,
    rewrite,
)

#: The exact content LangGraph wrote into the wedged thread.
WEDGED_BODY = (
    "Tool call tr_run_python with id e22ccd65-22a8-463a-9c16-bce6e437d2b0 "
    "was cancelled - another message came in before it could be completed."
)


def wedged_history() -> list:
    """The message shape recovered from the real thread, in order."""
    return [
        HumanMessage(content="can we do it through CUA"),
        AIMessage(content="", tool_calls=[{
            "id": "e22ccd65-22a8-463a-9c16-bce6e437d2b0",
            "name": "tr_run_python",
            "args": {"script_path": "/tmp/scrape_all_songs.py"},
            "type": "tool_call",
        }]),
        ToolMessage(
            id="msg-tool-1",
            tool_call_id="e22ccd65-22a8-463a-9c16-bce6e437d2b0",
            name="tr_run_python",
            status="success",
            content=WEDGED_BODY,
        ),
        HumanMessage(content="you have been just opening the app on browser"),
    ]


class Detection(unittest.TestCase):
    def test_the_real_wedged_history_is_detected(self):
        self.assertTrue(needs_repair(wedged_history()))

    def test_the_marker_still_matches_what_the_framework_writes(self):
        # If LangGraph rewords this, detection silently stops working and
        # threads wedge again. Fail loudly here instead.
        self.assertIn(CANCELLED_TOOL_MARKER, WEDGED_BODY)

    def test_a_clean_history_needs_nothing(self):
        clean = [HumanMessage(content="hi"), AIMessage(content="hello")]
        self.assertFalse(needs_repair(clean))
        self.assertEqual(rewrite(clean, Cause.INTERRUPTED_BY_USER), [])

    def test_a_successful_tool_result_is_left_alone(self):
        msgs = [ToolMessage(id="t", tool_call_id="c", name="tr_ls",
                            status="success", content='{"status": "ok"}')]
        self.assertFalse(needs_repair(msgs))

    def test_non_string_content_does_not_crash_detection(self):
        msgs = [ToolMessage(id="t", tool_call_id="c", name="x",
                            content=[{"type": "text", "text": "hi"}])]
        self.assertFalse(needs_repair(msgs))


class Rewriting(unittest.TestCase):
    def test_the_fabricated_success_becomes_an_explicit_error(self):
        out = rewrite(wedged_history(), Cause.INTERRUPTED_BY_USER)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].status, "error")
        self.assertNotIn(CANCELLED_TOOL_MARKER, out[0].content)

    def test_the_id_is_preserved_so_the_reducer_merges_in_place(self):
        # A new id would append a SECOND result for one tool call, which is a
        # worse history than the one being repaired.
        out = rewrite(wedged_history(), Cause.INTERRUPTED_BY_USER)
        self.assertEqual(out[0].id, "msg-tool-1")
        self.assertEqual(
            out[0].tool_call_id, "e22ccd65-22a8-463a-9c16-bce6e437d2b0")

    def test_the_tool_name_survives(self):
        out = rewrite(wedged_history(), Cause.INTERRUPTED_BY_USER)
        self.assertEqual(out[0].name, "tr_run_python")

    def test_an_interrupted_call_says_it_did_not_run(self):
        body = rewrite(wedged_history(), Cause.INTERRUPTED_BY_USER)[0].content
        self.assertIn("INTERRUPTED", body)
        self.assertIn("did NOT run", body)
        # The model must not conclude the work happened.
        self.assertIn("Do NOT report it as done", body)

    def test_a_dead_session_says_consent_was_never_given(self):
        body = rewrite(wedged_history(), Cause.SESSION_ENDED)[0].content
        self.assertIn("DENIED", body)
        self.assertIn("re-propose", body)

    def test_the_two_causes_tell_different_stories(self):
        a = rewrite(wedged_history(), Cause.INTERRUPTED_BY_USER)[0].content
        b = rewrite(wedged_history(), Cause.SESSION_ENDED)[0].content
        self.assertNotEqual(a, b)

    def test_several_cancelled_calls_are_all_repaired(self):
        msgs = wedged_history() + [
            ToolMessage(id="msg-tool-2", tool_call_id="c2", name="tr_ls",
                        status="success", content=WEDGED_BODY),
        ]
        self.assertEqual(len(rewrite(msgs, Cause.INTERRUPTED_BY_USER)), 2)


class _FakeAgent:
    """Minimal `aget_state`/`aupdate_state` surface, with failure modes."""

    def __init__(self, messages, *, read_fails=False, write_fails=False):
        self._messages = messages
        self.updates: list = []
        self._read_fails = read_fails
        self._write_fails = write_fails

    async def aget_state(self, config):
        if self._read_fails:
            raise RuntimeError("checkpoint unreachable")
        return type("S", (), {"values": {"messages": self._messages}})()

    async def aupdate_state(self, config, update):
        if self._write_fails:
            raise RuntimeError("checkpoint read-only")
        self.updates.append(update)


class RepairingAThread(unittest.TestCase):
    def test_it_writes_the_patched_messages_back(self):
        agent = _FakeAgent(wedged_history())
        n = asyncio.run(repair_orphan_tool_calls(agent, "t1"))
        self.assertEqual(n, 1)
        self.assertEqual(len(agent.updates), 1)
        self.assertEqual(agent.updates[0]["messages"][0].status, "error")

    def test_a_clean_thread_is_not_written_to_at_all(self):
        agent = _FakeAgent([HumanMessage(content="hi")])
        self.assertEqual(asyncio.run(repair_orphan_tool_calls(agent, "t1")), 0)
        self.assertEqual(agent.updates, [])

    def test_an_unreadable_checkpoint_returns_zero_rather_than_raising(self):
        # The caller's next step is to tell the user something went wrong;
        # that must not itself be prevented by an exception here.
        agent = _FakeAgent(wedged_history(), read_fails=True)
        self.assertEqual(asyncio.run(repair_orphan_tool_calls(agent, "t1")), 0)

    def test_a_failed_write_returns_zero_rather_than_raising(self):
        agent = _FakeAgent(wedged_history(), write_fails=True)
        self.assertEqual(asyncio.run(repair_orphan_tool_calls(agent, "t1")), 0)


class EmptyOutputIsANotice(unittest.TestCase):
    """The warnings the app dropped are now an undroppable kind."""

    def test_streaming_emits_notice_not_log_for_an_empty_turn(self):
        import inspect

        from yuyutsava.core import streaming

        src = inspect.getsource(streaming)
        self.assertIn('StreamEvent("notice"', src)
        self.assertIn("Agent produced no output", src)
        # The old droppable form must be gone for these two.
        self.assertNotIn(
            'StreamEvent("log", {\n            "text": (\n                f"⚠️  Agent produced no output',
            src,
        )

    def test_the_kind_is_documented_with_the_distinction(self):
        from yuyutsava.core.streaming import StreamEvent

        self.assertIn("notice", StreamEvent.__doc__)
        self.assertIn("may never be", StreamEvent.__doc__)


class TheResumePathStillWorks(unittest.TestCase):
    def test_the_cli_resume_helper_delegates_with_the_session_cause(self):
        import inspect

        from yuyutsava.sessions import runner

        src = inspect.getsource(runner._patch_orphan_cancellations)
        self.assertIn("SESSION_ENDED", src)
        self.assertIn("repair_orphan_tool_calls", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
