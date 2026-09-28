"""A one-shot turn repairs its history first and never ends in silence (R-BUG-06).

``yuyutsava "<task>"`` ran its turn without the pre-turn repair and the
empty-turn retry the REPL, the app and voice get from
``conversation.recovery``. After an interrupted session, a one-shot resume
could be sent into a history the model refuses, or end with no text and no
word about why.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from yuyutsava.cli import session_run
from yuyutsava.conversation.repair import is_internal
from yuyutsava.storage.sessions import SqliteSessionStore


class _Agent:
    """Checkpoint state only: the history a previous, interrupted turn left."""

    def __init__(self, messages: list) -> None:
        self._messages = messages
        self.updates: list = []

    async def aget_state(self, _config):
        return type("S", (), {"values": {"messages": self._messages}})()

    async def aupdate_state(self, _config, update, *args, **kwargs):
        self.updates.append(update)


def _turn_left_open() -> list:
    return [
        HumanMessage(content="read it", id="h1"),
        AIMessage(
            content="",
            id="a1",
            tool_calls=[{"id": "tc-1", "name": "tr_read_file", "args": {"path": "/x"}}],
        ),
        ToolMessage(content="contents", tool_call_id="tc-1", name="tr_read_file", id="t1"),
    ]


class OneShotRecovery(unittest.TestCase):
    def _run(self, agent, replies):
        calls: list = []

        async def fake_stream(agent_, task, **kwargs):
            calls.append(task)
            return replies[len(calls) - 1]

        err = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(session_run, "astream_agent", fake_stream),
            contextlib.redirect_stderr(err),
        ):
            store = SqliteSessionStore(Path(tmp) / "sessions.db")
            final = asyncio.run(
                session_run.run_session(store=store, agent=agent, task="go on", workspace=Path(tmp))
            )
        return final, calls, err.getvalue()

    def test_an_empty_turn_is_retried_once_without_a_new_message(self) -> None:
        final, calls, err = self._run(_Agent([HumanMessage(content="hi", id="h")]), ["", "done"])
        self.assertEqual(final, "done")
        self.assertEqual(calls, ["go on", None])
        self.assertIn("Retrying", err)

    def test_a_still_empty_re_run_gets_one_hidden_nudge(self) -> None:
        final, calls, _err = self._run(
            _Agent([HumanMessage(content="hi", id="h")]), ["", "", "done"]
        )
        self.assertEqual(final, "done")
        self.assertEqual(calls[:2], ["go on", None])
        self.assertTrue(
            is_internal(calls[2].additional_kwargs), "the nudge must be marked internal"
        )

    def test_empty_after_the_nudge_ends_in_a_visible_error(self) -> None:
        final, calls, err = self._run(_Agent([HumanMessage(content="hi", id="h")]), ["", "", ""])
        self.assertEqual(final, "")
        self.assertEqual(len(calls), 3, "the first turn, the re-run and the nudge — never a loop")
        self.assertIn("could not answer", err)

    def test_an_open_turn_is_closed_before_the_new_message(self) -> None:
        agent = _Agent(_turn_left_open())
        final, _calls, err = self._run(agent, ["ok"])
        self.assertEqual(final, "ok")
        self.assertTrue(agent.updates, "the unfinished turn was sent into the model as-is")
        self.assertIn("Picking up after an interruption", err)


if __name__ == "__main__":
    unittest.main()
