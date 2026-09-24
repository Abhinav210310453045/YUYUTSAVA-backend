"""What the chat REPL does with each kind of line, through `ReplCommands.dispatch`.

`test_slash_commands` checks that every advertised command is *named* by some
dispatcher, by reading source. These drive `dispatch` itself, so they say what
each command does with its arguments — the part a parsing change could break
while the source still names every command.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from yuyutsava.cli.commands import chat_repl as cr

Outcome = cr.Outcome


class _Ring:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def print_all(self) -> None:
        self.calls.append(("all",))

    def print_last(self, k: int) -> None:
        self.calls.append(("last", k))

    def print_one(self, i: int) -> None:
        self.calls.append(("one", i))


class _Toggles:
    """RuntimeSettings stand-in: records writes, reports what was written."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._voice = SimpleNamespace(wake_enabled=False, tts_enabled=False)
        self._disabled: set[str] = set()

    async def refresh(self, force: bool = False) -> None:
        self.calls.append(("refresh", force))

    async def set_voice(self, **kw: bool) -> None:
        self.calls.append(("set_voice", kw))
        for k, v in kw.items():
            setattr(self._voice, k, v)

    async def set_subagent_enabled(self, name: str, flag: bool) -> None:
        self.calls.append(("set_subagent", name, flag))
        (self._disabled.discard if flag else self._disabled.add)(name)

    def voice(self) -> Any:
        return self._voice

    def subagents(self) -> Any:
        return SimpleNamespace(disabled=set(self._disabled))


class _Remote:
    def __init__(self, pending: list[dict] | None = None) -> None:
        self.pending = pending or []
        self.answers: list[tuple[str, str]] = []

    def list_pending(self) -> list[dict]:
        return list(self.pending)

    async def answer(self, ask_id: str, text: str) -> str:
        self.answers.append((ask_id, text))
        if self.pending:
            self.pending.pop(0)
        return "answered"


def _commands(remote: _Remote | None = None) -> tuple[cr.ReplCommands, _Ring, _Toggles]:
    ring, toggles = _Ring(), _Toggles()

    async def _get() -> _Toggles:
        return toggles

    cmds = cr.ReplCommands(
        workspace=Path("/ws"),
        console=None,
        renderer=SimpleNamespace(ring=ring),
        bundle=SimpleNamespace(usage_store="USAGE", layout="LAYOUT"),
        convo=None,
        runtime_toggles=SimpleNamespace(get=_get),
        status_line="",
        session=SimpleNamespace(id="sess-1", thread_id="thr-1", created_at=123.0),
        remote=remote,
    )
    return cmds, ring, toggles


def _dispatch(cmds: cr.ReplCommands, text: str) -> tuple[Outcome, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = asyncio.run(asyncio.wait_for(cmds.dispatch(text), 10))
    return out, err.getvalue()


class NotACommand(unittest.TestCase):
    def test_ordinary_text_goes_to_the_agent(self) -> None:
        cmds, ring, toggles = _commands()
        for text in ("hello", "", "   ", "say /quit please", "y"):
            with self.subTest(text=text):
                self.assertEqual(_dispatch(cmds, text)[0], Outcome.NOT_A_COMMAND)
        self.assertEqual((ring.calls, toggles.calls), ([], []))


class Quitting(unittest.TestCase):
    def test_every_alias_any_case_any_padding(self) -> None:
        cmds, *_ = _commands()
        for text in ("/quit", "/exit", "/q", "  /EXIT  ", "/Quit now"):
            with self.subTest(text=text):
                self.assertEqual(_dispatch(cmds, text)[0], Outcome.QUIT)


class ScrollbackCommands(unittest.TestCase):
    def test_ring_last_and_expand(self) -> None:
        cmds, ring, _ = _commands()
        for text in ("/ring", "/last", "/last 3", "/expand 2", "  /LAST   4  "):
            self.assertEqual(_dispatch(cmds, text)[0], Outcome.HANDLED)
        self.assertEqual(ring.calls, [("all",), ("last", 1), ("last", 3), ("one", 2), ("last", 4)])

    def test_bad_arguments_say_why_and_do_nothing(self) -> None:
        cmds, ring, _ = _commands()
        cases = {
            "/last x": "/last: expected integer, got 'x'",
            "/expand": "/expand: usage: /expand <n>",
            "/expand two": "/expand: expected integer, got 'two'",
        }
        for text, said in cases.items():
            with self.subTest(text=text):
                outcome, err = _dispatch(cmds, text)
                self.assertEqual(outcome, Outcome.HANDLED)
                self.assertIn(said, err)
        self.assertEqual(ring.calls, [])

    def test_session_and_unknown(self) -> None:
        cmds, *_ = _commands()
        self.assertIn("sess-1", _dispatch(cmds, "/session")[1])
        outcome, err = _dispatch(cmds, "/bogus arg")
        self.assertEqual(outcome, Outcome.HANDLED)
        self.assertIn("unknown command: /bogus (try /help)", err)


class Reports(unittest.TestCase):
    def test_usage_context_skills_and_help(self) -> None:
        cmds, *_ = _commands()
        with mock.patch.object(cr, "reports") as reports:
            reports.print_usage_summary = mock.AsyncMock()
            for text in ("/usage", "/context", "/skills", "/help"):
                self.assertEqual(_dispatch(cmds, text)[0], Outcome.HANDLED)
        reports.print_usage_summary.assert_awaited_once_with("USAGE", "thr-1", 123.0)
        reports.print_context.assert_called_once_with("thr-1", None)
        reports.print_skills.assert_called_once_with("LAYOUT")
        reports.print_help.assert_called_once_with(cr.SLASH_COMMANDS)

    def test_new_rotates_the_session(self) -> None:
        cmds, *_ = _commands()
        with mock.patch.object(cr.ReplCommands, "_rotate_session", mock.AsyncMock()) as rotate:
            self.assertEqual(_dispatch(cmds, "/NEW")[0], Outcome.HANDLED)
        rotate.assert_awaited_once()


class RuntimeToggles(unittest.TestCase):
    def test_voice(self) -> None:
        cmds, _, toggles = _commands()
        _dispatch(cmds, "/voice on")
        _dispatch(cmds, "/Voice TTS Off")
        _, err = _dispatch(cmds, "/voice")
        self.assertEqual(
            [c for c in toggles.calls if c[0] == "set_voice"],
            [
                ("set_voice", {"wake_enabled": True, "tts_enabled": True}),
                ("set_voice", {"tts_enabled": False}),
            ],
        )
        self.assertIn("spoken reply:\x1b[0m off", err)
        self.assertEqual(toggles.calls.count(("refresh", True)), 3)

    def test_voice_usage(self) -> None:
        cmds, _, toggles = _commands()
        _, err = _dispatch(cmds, "/voice loud")
        self.assertIn("usage: /voice", err)
        self.assertFalse([c for c in toggles.calls if c[0] == "set_voice"])

    def test_subagents(self) -> None:
        cmds, _, toggles = _commands()
        _, err = _dispatch(cmds, "/subagents off researcher")
        self.assertIn("researcher", err)
        _, err = _dispatch(cmds, "/subagents off general-purpose")
        self.assertIn("can't be switched off", err)
        _, err = _dispatch(cmds, "/subagents off")
        self.assertIn("usage: /subagents", err)
        self.assertEqual(
            [c for c in toggles.calls if c[0] == "set_subagent"],
            [("set_subagent", "researcher", False)],
        )


class Approvals(unittest.TestCase):
    def test_asks_approve_reject_reply(self) -> None:
        remote = _Remote([{"ask_id": "abcdef123456", "title": "Run rm"}])
        cmds, *_ = _commands(remote)
        _, err = _dispatch(cmds, "/asks")
        self.assertIn("abcdef12  Run rm", err)
        for text in ("/approve", "/REJECT abc", "/reply ID1 Keep  Its Case", "/reply id1"):
            self.assertEqual(_dispatch(cmds, text)[0], Outcome.HANDLED)
        self.assertEqual(
            remote.answers,
            [("", "approve"), ("abc", "reject"), ("ID1", "Keep Its Case")],
        )

    def test_a_bare_decision_answers_only_while_something_is_pending(self) -> None:
        remote = _Remote([{"ask_id": "a1", "title": "t"}])
        cmds, *_ = _commands(remote)
        self.assertEqual(_dispatch(cmds, "y")[0], Outcome.HANDLED)
        self.assertEqual(_dispatch(cmds, "y")[0], Outcome.NOT_A_COMMAND)
        self.assertEqual(remote.answers, [("", "approve")])

    def test_without_a_daemon_approval_commands_are_unknown(self) -> None:
        cmds, *_ = _commands(remote=None)
        self.assertIn("unknown command: /approve", _dispatch(cmds, "/approve")[1])


if __name__ == "__main__":
    unittest.main()
