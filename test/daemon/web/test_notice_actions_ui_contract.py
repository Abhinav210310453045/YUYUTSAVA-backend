"""The app offers exactly the notice actions the daemon sends (R-UI-11).

The daemon names a notice's actions (``conversation.recovery.NOTICE_ACTIONS``);
the app turns each into a button (``conversations/noticeActions.js``) and the
REPL into a command (``cli/render/notices.ACTION_COMMANDS``). An action the app
does not know is simply not offered — no error, no button — so a new action
added here would silently never reach the app. And a typed command means the
same in both clients, or "/new" does one thing in the terminal and another in
the window.

The JS is read as text: the two constants are plain literals by design.

Run:  .venv/bin/python -m pytest test/daemon/web/test_notice_actions_ui_contract.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from yuyutsava.cli.render.notices import ACTION_COMMANDS
from yuyutsava.conversation.recovery import NOTICE_ACTIONS

JS = (
    Path(__file__).resolve().parents[3] / "electron-app/src/renderer/conversations/noticeActions.js"
)


def app_action_ids(source: str) -> set[str]:
    """The ids in ``NOTICE_ACTION_IDS = Object.freeze([...])``."""
    m = re.search(r"NOTICE_ACTION_IDS\s*=\s*Object\.freeze\(\[([^\]]*)\]\)", source)
    if m is None:
        raise AssertionError("noticeActions.js no longer declares NOTICE_ACTION_IDS")
    return set(re.findall(r"'([^']+)'", m.group(1)))


def app_commands(source: str) -> dict[str, str]:
    """``{action id: command}`` from ``COMMANDS = Object.freeze({...})``."""
    m = re.search(r"COMMANDS\s*=\s*Object\.freeze\(\{([^}]*)\}\)", source)
    if m is None:
        raise AssertionError("noticeActions.js no longer declares COMMANDS")
    return {action: cmd for cmd, action in re.findall(r"'([^']+)':\s*'([^']+)'", m.group(1))}


class TheAppAndTheDaemonAgree(unittest.TestCase):
    def test_every_action_the_daemon_sends_is_one_the_app_offers(self) -> None:
        self.assertEqual(app_action_ids(JS.read_text()), {a.id for a in NOTICE_ACTIONS})

    def test_a_typed_command_means_the_same_in_the_app_and_the_repl(self) -> None:
        self.assertEqual(app_commands(JS.read_text()), ACTION_COMMANDS)


class TheCheckFails(unittest.TestCase):
    """Negative controls: a drifted file is read as drifted, a rewritten one is named."""

    def test_a_missing_id_is_seen(self) -> None:
        self.assertEqual(
            app_action_ids("export const NOTICE_ACTION_IDS = Object.freeze(['retry'])"),
            {"retry"},
        )

    def test_a_rewritten_constant_is_reported(self) -> None:
        with self.assertRaisesRegex(AssertionError, "NOTICE_ACTION_IDS"):
            app_action_ids("export const IDS = ['retry']")


if __name__ == "__main__":
    unittest.main()
