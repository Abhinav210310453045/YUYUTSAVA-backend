"""What a person is shown before they approve something, and what their
answer is taken to mean.

`R-DRY-05`: two surfaces ask — the REPL's bridge for a paused background
subagent, and `yuyutsava attach` for a question off a running daemon's stream
— and each had its own copy. The copies had drifted: one showed the ask id and
marked background askers, the other showed neither, for the same question about
the same operation.

Duplication is usually a maintenance cost. Here it is a correctness one: this
is the prompt an action is approved at, so two renderings mean what a person
sees depends on which surface happened to ask them.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import unittest
from unittest import mock

from yuyutsava.cli.ask_prompt import (
    AskView,
    permission_keys,
    prompt_for_ask,
    render_ask,
)


def _shown(view: AskView) -> str:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        render_ask(view)
    return err.getvalue()


class WhatIsShown(unittest.TestCase):
    def test_the_question_and_who_is_asking(self) -> None:
        out = _shown(AskView(title="Run this?", body="rm -rf /tmp/x", agent_path="orchestrator"))
        self.assertIn("Run this?", out)
        self.assertIn("rm -rf /tmp/x", out)
        self.assertIn("orchestrator", out)

    def test_a_background_asker_is_marked(self) -> None:
        """Otherwise a prompt appears with no sign it belongs to a task the
        user started minutes ago and has stopped watching."""
        out = _shown(AskView(title="Q", agent_path="orchestrator/file_organizer#bg"))
        self.assertIn("[background]", out)

    def test_a_foreground_asker_is_not_marked(self) -> None:
        self.assertNotIn("[background]", _shown(AskView(title="Q", agent_path="orchestrator")))

    def test_the_ask_id_is_shown_when_there_is_one(self) -> None:
        """It is what `/reply <id>` takes, so a user who steps away can still
        answer."""
        self.assertIn("ask=abcd1234", _shown(AskView(title="Q", ask_id="abcd1234efgh")))

    def test_nothing_empty_is_drawn(self) -> None:
        out = _shown(AskView(title="Q"))
        self.assertNotIn("from:", out)
        self.assertNotIn("ask=", out)
        self.assertNotIn("options:", out)

    def test_a_permission_ask_offers_yes_and_no(self) -> None:
        out = _shown(AskView(title="Q", options=["approve", "reject"]))
        self.assertIn("[y]es", out)
        self.assertIn("[n]o", out)

    def test_scope_is_offered_only_when_it_is_available(self) -> None:
        """Offering [p]roject when the grant cannot be stored would teach the
        user a key that does nothing."""
        self.assertNotIn("[p]roject", _shown(AskView(title="Q", options=["approve", "reject"])))
        self.assertIn(
            "[p]roject",
            _shown(AskView(title="Q", options=["approve", "reject", "project"])),
        )

    def test_a_free_text_ask_lists_its_options(self) -> None:
        out = _shown(AskView(title="Pick", options=["red", "blue"]))
        self.assertIn("red / blue", out)
        self.assertNotIn("[y]es", out)


class WhatAnAnswerMeans(unittest.IsolatedAsyncioTestCase):
    async def _answer(self, view: AskView, typed: str | None) -> str:
        with mock.patch("yuyutsava.cli.ask_prompt.read_line", return_value=typed):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                return await prompt_for_ask(view)

    PERMISSION = AskView(title="Q", options=["approve", "reject"])

    async def test_yes_approves(self) -> None:
        self.assertEqual(await self._answer(self.PERMISSION, "y"), "approve")

    async def test_no_rejects(self) -> None:
        self.assertEqual(await self._answer(self.PERMISSION, "n"), "reject")

    async def test_an_unreadable_answer_is_a_refusal(self) -> None:
        """Not an approval, and not a crash: a key nobody meant must not run
        the command."""
        self.assertEqual(await self._answer(self.PERMISSION, "maybe?"), "reject")

    async def test_end_of_input_is_a_refusal(self) -> None:
        """Ctrl+D, or a surface that went away. Never consent."""
        self.assertEqual(await self._answer(self.PERMISSION, None), "reject")

    async def test_an_empty_line_is_a_refusal(self) -> None:
        self.assertEqual(await self._answer(self.PERMISSION, ""), "reject")

    async def test_free_text_is_passed_through(self) -> None:
        view = AskView(title="Which?", options=["red", "blue"])
        self.assertEqual(await self._answer(view, "red"), "red")

    async def test_free_text_end_of_input_is_still_a_refusal(self) -> None:
        view = AskView(title="Which?", options=["red", "blue"])
        self.assertEqual(await self._answer(view, None), "reject")


class NobodyKeepsTheirOwnCopy(unittest.TestCase):
    """Three surfaces mention this question. Two block for an answer and must
    ask identically; the third only announces a queued ask and says how to
    answer it later — a different rendering, but not a licence to offer
    different answer keys."""

    ROOT = pathlib.Path(__file__).resolve().parent.parent.parent / "yuyutsava" / "cli"

    def test_the_two_blocking_surfaces_share_one_prompt(self) -> None:
        for name in ("async_hitl.py", "remote_attach.py"):
            source = (self.ROOT / name).read_text()
            with self.subTest(module=name):
                self.assertIn("prompt_for_ask", source)
                self.assertNotIn("approve/reject> ", source, f"{name} still builds its own prompt")

    def test_the_terse_keys_are_spelled_in_one_place(self) -> None:
        """There are two vocabularies on purpose — terse for the background and
        attach prompts, and the longer "[y] allow once" wording on the
        foreground card, which has room. Each must have one spelling: a retry
        hint once mixed them, and offered [s]ession and [p]roject for
        interrupts that cannot store a grant."""
        offenders = [
            str(path.relative_to(self.ROOT.parent))
            for path in sorted(self.ROOT.rglob("*.py"))
            if path.name != "ask_prompt.py" and "[y]es" in path.read_text()
        ]
        self.assertEqual(offenders, [], f"terse keys spelled out in {offenders}")

    def test_the_announcement_offers_the_same_keys(self) -> None:
        """A user told "[y]es / [n]o" by one surface and something else by
        another does not know which key approves."""
        self.assertEqual(permission_keys(["approve", "reject"]), "[y]es / [n]o")
        self.assertEqual(
            permission_keys(["approve", "reject", "session", "project"]),
            "[y]es / [n]o / [s]ession / [p]roject",
        )


if __name__ == "__main__":
    unittest.main()
