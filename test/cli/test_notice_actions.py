"""A notice's actions reach the terminal as commands the REPL has (R-UI-11).

The app offers a notice's actions as buttons; the REPL, as the slash commands
that do them. The table between the two (`render.notices.ACTION_COMMANDS`) is
pinned from both sides: every action a notice can carry has a command, and
every command it names is one `/help` lists — so the hint can never again point
at a command that does not exist where it is shown.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import unittest
from pathlib import Path

from yuyutsava.cli.commands.chat_repl import SLASH_COMMANDS
from yuyutsava.cli.render.console import make_console
from yuyutsava.cli.render.notices import ACTION_COMMANDS, action_hint
from yuyutsava.cli.render.plain import ChatRenderer
from yuyutsava.cli.render.renderer import RichChatRenderer
from yuyutsava.conversation.recovery import NEW_CHAT, NOTICE_ACTIONS, RETRY
from yuyutsava.core.streaming import StreamEvent

GAVE_UP = {
    "level": "error",
    "text": "The model could not answer.",
    "code": "empty_turn",
    "actions": [RETRY.wire(), NEW_CHAT.wire()],
}


def missing_commands(table: dict[str, str], commands: set[str]) -> dict[str, str]:
    """Entries of *table* whose command is not one of *commands*."""
    return {action: cmd for action, cmd in table.items() if cmd not in commands}


class TheTableIsPinned(unittest.TestCase):
    def test_every_action_has_a_command(self) -> None:
        self.assertEqual({a.id for a in NOTICE_ACTIONS}, set(ACTION_COMMANDS))

    def test_every_command_is_a_real_slash_command(self) -> None:
        self.assertEqual(missing_commands(ACTION_COMMANDS, {c.name for c in SLASH_COMMANDS}), {})

    def test_negative_control_a_missing_command_is_named(self) -> None:
        self.assertEqual(
            missing_commands({"retry": "/again"}, {"/retry"}),
            {"retry": "/again"},
        )


class TheHint(unittest.TestCase):
    def test_actions_become_commands_in_order(self) -> None:
        self.assertEqual(action_hint(GAVE_UP), "→ /retry · /new")

    def test_no_actions_or_unknown_ones_give_no_hint(self) -> None:
        self.assertEqual(action_hint({"text": "x"}), "")
        self.assertEqual(action_hint({"actions": [{"id": "dance"}, "junk"]}), "")


def _render(renderer, data: dict) -> str:
    buf = io.StringIO()

    async def _run() -> None:
        renderer.begin_turn()
        await renderer.render(StreamEvent("notice", data))
        await renderer.end_of_turn()

    with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
        asyncio.run(_run())
    return buf.getvalue()


class BothRenderersShowIt(unittest.TestCase):
    def test_plain_and_rich_print_the_text_and_the_commands(self) -> None:
        for name in ("plain", "rich"):
            with self.subTest(renderer=name):
                console_buf = io.StringIO()
                renderer = (
                    ChatRenderer(verbose=True, workspace=Path("/tmp"))
                    if name == "plain"
                    else RichChatRenderer(
                        verbose=True,
                        workspace=Path("/tmp"),
                        console=make_console(file=console_buf, width=100, force_terminal=False),
                    )
                )
                written = _render(renderer, GAVE_UP) + console_buf.getvalue()
                self.assertIn("The model could not answer.", written)
                self.assertIn("→ /retry · /new", written)


if __name__ == "__main__":
    unittest.main()
