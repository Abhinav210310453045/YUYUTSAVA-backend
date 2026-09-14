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
    diagnose,
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

    def test_a_successful_tool_result_is_never_rewritten(self):
        # It IS reported as an unclosed turn — the assistant never got to say
        # what it concluded — but the result itself must not be touched.
        msgs = [ToolMessage(id="t", tool_call_id="c", name="tr_ls",
                            status="success", content='{"status": "ok"}')]
        found = diagnose(msgs)
        self.assertEqual(found.cancelled, ())
        self.assertEqual(found.dangling, ())
        self.assertTrue(found.unclosed_turn)
        # Nothing to repair if the turn is not ours to close (a resume).
        self.assertFalse(needs_repair(msgs, close_turn=False))

    def test_non_string_content_does_not_crash_detection(self):
        msgs = [ToolMessage(id="t", tool_call_id="c", name="x",
                            content=[{"type": "text", "text": "hi"}])]
        self.assertEqual(diagnose(msgs).cancelled, ())


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
        out = rewrite(msgs, Cause.INTERRUPTED_BY_USER)
        rewritten = {m.id for m in out if isinstance(m, ToolMessage)}
        self.assertEqual(rewritten, {"msg-tool-1", "msg-tool-2"})


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


# ----------------------------------------------------------------------
# 14 Sep: the graceful interrupt left the assistant's turn hanging open
# ----------------------------------------------------------------------

def interrupted_history() -> list:
    """The tail measured from thread ``cli-1789337564-…``, seq 6400-6401.

    The model opened its turn with a tool call, the tool answered — and the
    user's Ctrl+S message was about to be appended straight after the result,
    which is the shape that came back empty twice at ~61k input tokens.
    """
    return [
        HumanMessage(content="where is the turn driven"),
        AIMessage(content="", id="ai-6400", tool_calls=[{
            "id": "2a294c14",
            "name": "tr_grep",
            "args": {"pattern": "(class |async def )"},
            "type": "tool_call",
        }]),
        ToolMessage(
            id="tool-6401", tool_call_id="2a294c14", name="tr_grep",
            status="success",
            content='{"status":"success","result":"167 matching lines"}',
        ),
    ]


class TheUnfinishedTurn(unittest.TestCase):
    """`repaired=0` was honest: neither earlier shape was present."""

    def test_the_old_checks_find_nothing_here(self):
        found = diagnose(interrupted_history())
        self.assertEqual(found.cancelled, ())
        self.assertEqual(found.dangling, ())

    def test_but_the_turn_is_reported_as_unclosed(self):
        self.assertTrue(diagnose(interrupted_history()).unclosed_turn)

    def test_the_repair_closes_it_with_an_assistant_message(self):
        out = rewrite(interrupted_history(), Cause.INTERRUPTED_BY_USER)
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[-1], AIMessage)
        self.assertIn("interrupted", out[-1].content)

    def test_the_closer_is_the_last_message_so_the_user_follows_a_model_turn(self):
        msgs = interrupted_history()
        msgs[-1] = ToolMessage(
            id="tool-x", tool_call_id="2a294c14", name="tr_grep",
            status="success", content=WEDGED_BODY,
        )
        out = rewrite(msgs, Cause.INTERRUPTED_BY_USER)
        # The rewritten tool result first, the closing turn last.
        self.assertIsInstance(out[0], ToolMessage)
        self.assertIsInstance(out[-1], AIMessage)

    def test_a_resume_does_not_declare_the_turn_interrupted(self):
        # Answering a permission prompt continues the SAME turn; saying it was
        # interrupted would be a lie the model then reasons from.
        out = rewrite(
            interrupted_history(), Cause.INTERRUPTED_BY_USER, close_turn=False)
        self.assertEqual(out, [])

    def test_a_history_ending_on_the_users_turn_is_already_closed(self):
        msgs = interrupted_history() + [HumanMessage(content="never mind")]
        self.assertFalse(diagnose(msgs).unclosed_turn)

    def test_the_two_causes_close_the_turn_differently(self):
        a = rewrite(interrupted_history(), Cause.INTERRUPTED_BY_USER)[-1].content
        b = rewrite(interrupted_history(), Cause.SESSION_ENDED)[-1].content
        self.assertNotEqual(a, b)
        self.assertIn("user interrupted", a)
        self.assertIn("session ended", b)


class EmptyAssistantMessages(unittest.TestCase):
    """An empty reply is poison, and it accumulates."""

    def test_an_empty_reply_is_removed_by_id(self):
        from langchain_core.messages import RemoveMessage

        msgs = interrupted_history() + [
            HumanMessage(content="no need to study web it is outdated"),
            AIMessage(content="", id="ai-6403"),
        ]
        out = rewrite(msgs, Cause.INTERRUPTED_BY_USER)
        removals = [m for m in out if isinstance(m, RemoveMessage)]
        self.assertEqual([m.id for m in removals], ["ai-6403"])

    def test_both_empty_replies_from_the_real_thread_are_removed(self):
        msgs = interrupted_history() + [
            HumanMessage(content="no need to study web it is outdated"),
            AIMessage(content="", id="ai-6403"),
            HumanMessage(content="get it now?"),
            AIMessage(content="", id="ai-6405"),
        ]
        self.assertEqual(
            list(diagnose(msgs).empty_assistant), ["ai-6403", "ai-6405"])

    def test_a_tool_calling_reply_with_no_text_is_normal_and_kept(self):
        # Every tool call in the wedged histories looks like this. Removing
        # them would destroy the conversation.
        self.assertEqual(diagnose(interrupted_history()).empty_assistant, ())

    def test_block_list_content_with_text_is_not_empty(self):
        msgs = [AIMessage(content=[{"type": "text", "text": "hello"}], id="a1")]
        self.assertEqual(diagnose(msgs).empty_assistant, ())

    def test_whitespace_only_content_is_empty(self):
        msgs = [AIMessage(content="   \n  ", id="a1")]
        self.assertEqual(list(diagnose(msgs).empty_assistant), ["a1"])

    def test_removals_come_before_anything_appended(self):
        from langchain_core.messages import RemoveMessage

        msgs = wedged_history() + [
            AIMessage(content="", id="ai-empty"),
            ToolMessage(id="t9", tool_call_id="c9", name="tr_ls",
                        status="success", content="ok"),
        ]
        out = rewrite(msgs, Cause.INTERRUPTED_BY_USER)
        kinds = [type(m).__name__ for m in out]
        self.assertLess(
            kinds.index("RemoveMessage"), kinds.index("AIMessage"),
            f"removals must precede appends, got {kinds}",
        )


class DanglingToolCalls(unittest.TestCase):
    """A hard cancel can leave a call with no result at all."""

    def _dangling(self) -> list:
        return [
            HumanMessage(content="run it"),
            AIMessage(content="", id="ai-1", tool_calls=[{
                "id": "call-1", "name": "tr_run_python",
                "args": {"script_path": "/tmp/x.py"}, "type": "tool_call",
            }]),
        ]

    def test_a_call_with_no_result_anywhere_is_found(self):
        self.assertEqual(
            diagnose(self._dangling()).dangling, (("call-1", "tr_run_python"),))

    def test_a_result_is_synthesised_for_it(self):
        out = rewrite(self._dangling(), Cause.INTERRUPTED_BY_USER)
        tools = [m for m in out if isinstance(m, ToolMessage)]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].tool_call_id, "call-1")
        self.assertEqual(tools[0].status, "error")
        self.assertIn("did NOT run", tools[0].content)

    def test_the_synthesised_result_takes_a_fresh_id(self):
        # There is no existing message to merge with, so reusing the AI
        # message's id would clobber the call itself.
        out = rewrite(self._dangling(), Cause.INTERRUPTED_BY_USER)
        tools = [m for m in out if isinstance(m, ToolMessage)]
        self.assertNotEqual(tools[0].id, "ai-1")

    def test_an_answered_call_is_not_reported(self):
        self.assertEqual(diagnose(interrupted_history()).dangling, ())

    def test_one_answered_one_not_reports_only_the_orphan(self):
        msgs = [
            AIMessage(content="", id="ai-1", tool_calls=[
                {"id": "a", "name": "tr_ls", "args": {}, "type": "tool_call"},
                {"id": "b", "name": "tr_grep", "args": {}, "type": "tool_call"},
            ]),
            ToolMessage(id="t-a", tool_call_id="a", name="tr_ls",
                        status="success", content="ok"),
        ]
        self.assertEqual(diagnose(msgs).dangling, (("b", "tr_grep"),))


class AnEmptyTurnIsAlwaysRetried(unittest.TestCase):
    """The 14 Sep correction: `repaired=0` used to mean "give up".

    The log read ``(repaired=0) — surfaced to the user`` twice in a row, and
    the user's very next message went through untouched. The turn was
    recoverable and nothing tried.
    """

    def _service(self):
        from yuyutsava.conversation.service import ConversationService

        svc = ConversationService.__new__(ConversationService)
        # ``thread_id`` is a read-only property over the session row; the
        # recovery path only reads it, so a stub session is enough and keeps
        # this a unit test rather than a daemon boot.
        svc.session = type("S", (), {"thread_id": "t-empty"})()
        return svc

    def _run(self, messages):
        svc = self._service()
        bundle = type("B", (), {"agent": _FakeAgent(messages)})()
        events: list = []
        drives: list = []

        async def drive(user_text, resume):
            drives.append((user_text, resume))
            return "recovered", 3

        final, steps = asyncio.run(svc._recover_empty_turn(
            bundle, on_event=events.append, drive=drive,
        ))
        return final, steps, events, drives

    def test_a_clean_history_still_gets_its_one_retry(self):
        final, steps, _events, drives = self._run(
            [HumanMessage(content="hi")])
        self.assertEqual(final, "recovered")
        self.assertEqual(steps, 3)
        self.assertEqual(len(drives), 1, "exactly one retry, never a loop")

    def test_the_retry_sends_no_new_user_message(self):
        # Theirs is already in history; sending it twice makes the model
        # answer itself.
        _final, _steps, _events, drives = self._run(
            [HumanMessage(content="hi")])
        self.assertEqual(drives[0], (None, None))

    def test_the_user_is_told_a_retry_is_happening(self):
        _final, _steps, events, _drives = self._run(
            [HumanMessage(content="hi")])
        self.assertTrue(events, "silence is the one forbidden outcome")
        self.assertEqual(events[0].kind, "notice")
        self.assertIn("Retrying", events[0].data["text"])

    def test_a_repairable_history_says_what_it_repaired(self):
        _final, _steps, events, _drives = self._run(wedged_history())
        self.assertEqual(events[0].kind, "notice")
        self.assertIn("cancelled tool call", events[0].data["text"])

    def test_a_still_empty_retry_ends_in_a_visible_error(self):
        svc = self._service()
        bundle = type("B", (), {"agent": _FakeAgent([HumanMessage(content="hi")])})()
        events: list = []

        async def drive(user_text, resume):
            return "", 1

        final, _steps = asyncio.run(svc._recover_empty_turn(
            bundle, on_event=events.append, drive=drive,
        ))
        self.assertEqual(final, "")
        self.assertEqual(events[-1].data["level"], "error")
        self.assertIn("twice", events[-1].data["text"])

    def test_a_new_turn_closes_an_interrupted_one_but_a_resume_does_not(self):
        import inspect

        from yuyutsava.conversation.service import ConversationService

        src = inspect.getsource(ConversationService.run_turn)
        self.assertIn("close_turn=resume_value is None", src)


class TheResumePathStillWorks(unittest.TestCase):
    def test_the_cli_resume_helper_delegates_with_the_session_cause(self):
        import inspect

        from yuyutsava.sessions import runner

        src = inspect.getsource(runner._patch_orphan_cancellations)
        self.assertIn("SESSION_ENDED", src)
        self.assertIn("repair_orphan_tool_calls", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
