"""The terminal channel renders to the stream it is given and reads answers the same way.

The daemon's fallback surface for a foreground run without the app: proposals
and asks are printed and answered line by line. It had no tests because it wrote
to the real stderr and read the real stdin.
"""

from __future__ import annotations

import asyncio
import io
import unittest

from yuyutsava.daemon.terminal_channel import TerminalChannel
from yuyutsava.ports.channels import AskPrompt, ChannelEvent, TimelinePayload
from yuyutsava.storage.events import Proposal


def _channel(*answers: str) -> tuple[TerminalChannel, io.StringIO, list[str]]:
    out = io.StringIO()
    prompts: list[str] = []
    replies = iter(answers)

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    return TerminalChannel(out=out, ask=ask), out, prompts


def _proposal() -> Proposal:
    return Proposal.new(
        event_id="e",
        topic="fs.changed",
        summary="a pdf landed",
        proposed="file it under /docs",
        subagent="file-organizer",
        urgency=2,
        expiry_sec=600,
    )


class Proposals(unittest.TestCase):
    def test_modify_asks_for_the_new_instruction(self) -> None:
        ch, out, prompts = _channel("m", "file it under /papers")
        decision = asyncio.run(ch.post_proposal(_proposal()))
        self.assertEqual(decision.decision, "modify")
        self.assertEqual(decision.edited_instruction, "file it under /papers")
        self.assertEqual(prompts, ["  Decision: ", "  New instruction: "])
        self.assertIn("PROPOSAL  fs.changed", out.getvalue())
        self.assertIn("Proposed: file it under /docs", out.getvalue())

    def test_anything_unrecognised_is_a_skip(self) -> None:
        ch, _, _ = _channel("maybe")
        self.assertEqual(asyncio.run(ch.post_proposal(_proposal())).decision, "skip")


class Asks(unittest.TestCase):
    def test_a_permission_ask_offers_its_scopes_and_maps_the_answer(self) -> None:
        ch, out, prompts = _channel("session")
        ask = AskPrompt(
            ask_id="k",
            title="Run tr_execute?",
            body="ls ~",
            options=["approve", "reject", "session", "project"],
            interrupt_value=None,
        )
        self.assertEqual(asyncio.run(ch.post_ask(ask)), "approve_session")
        self.assertIn("[y]es / [n]o / [s]ession / [p]roject", out.getvalue())
        self.assertEqual(prompts, ["  Allow? [y/N]: "])

    def test_a_question_passes_the_answer_through(self) -> None:
        ch, _, _ = _channel("the blue one")
        ask = AskPrompt(ask_id="k", title="Which?", body="", options=[], interrupt_value=None)
        self.assertEqual(asyncio.run(ch.post_ask(ask)), "the blue one")


class Events(unittest.TestCase):
    def test_timeline_lines_go_to_the_given_stream(self) -> None:
        ch, out, _ = _channel()
        asyncio.run(ch.post_event(ChannelEvent(payload=TimelinePayload(line="task done"))))
        self.assertIn("• task done", out.getvalue())


if __name__ == "__main__":
    unittest.main()
