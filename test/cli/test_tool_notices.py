"""The chat says, once each and in plain words, when its MCP servers come up or give up."""

from __future__ import annotations

import contextlib
import io
import unittest

from yuyutsava.cli.tool_notices import ToolNotices
from yuyutsava.mcp.status import CONNECTED, FAILED, STARTING, ServerStatus


class _Manager:
    def __init__(self, **states: str) -> None:
        self.states = dict(states)
        self.errors: dict[str, str] = {}
        self.listeners: list = []

    def status(self) -> list[ServerStatus]:
        return [
            ServerStatus(name, state, error=self.errors.get(name, ""))
            for name, state in self.states.items()
        ]

    def subscribe(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def settle(self, name: str, state: str, error: str = "") -> None:
        self.states[name] = state
        self.errors[name] = error
        for listener in list(self.listeners):
            listener()


class _Dashboard:
    def __init__(self) -> None:
        self.shown: list[str] = []

    def notice(self, text: str, *, ttl: float) -> None:
        self.shown.append(text)


class InTheSplitView(unittest.TestCase):
    def test_each_change_goes_to_the_panel_once(self) -> None:
        manager = _Manager(drive=STARTING, blender=STARTING)
        dashboard = _Dashboard()
        notices = ToolNotices(manager, dashboard)
        manager.settle("drive", CONNECTED)
        manager.settle("drive", CONNECTED)  # nothing new: said once
        manager.settle("blender", FAILED, "did not start within 60 s")
        notices.stop()
        manager.settle("drive", STARTING)  # stopped: not heard
        self.assertEqual(
            dashboard.shown,
            [
                "starting your tools: drive, blender — each joins this chat when it is ready",
                "✓ drive is ready — its tools are part of this chat now",
                "✗ blender didn't start (did not start within 60 s) — "
                "its tools are off for this chat",
            ],
        )


class InTheClassicRepl(unittest.TestCase):
    def test_notices_wait_for_the_next_gap_between_turns(self) -> None:
        manager = _Manager(drive=STARTING)
        notices = ToolNotices(manager, None)
        manager.settle("drive", CONNECTED)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            notices.flush()
            notices.flush()  # already said
        said = err.getvalue()
        self.assertIn("starting your tools: drive", said)
        self.assertIn("✓ drive is ready", said)
        self.assertEqual(said.count("drive is ready"), 1)

    def test_says_nothing_when_every_server_was_already_up(self) -> None:
        notices = ToolNotices(_Manager(drive=CONNECTED), None)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            notices.flush()
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
