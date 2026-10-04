"""A model reads its own assistant turns as written, and everyone else's as text plus tool calls.

What crosses providers safely is pinned by
``test/framework_contract/test_cross_provider_history_contract.py``; these pin
who counts as the writer and what the portable form keeps.
"""

from __future__ import annotations

import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from yuyutsava.llm.quirks.history_portable import STAMP, for_reader, portable, stamped, writer_of

GEMINI = "work/gemini-3.5-flash"
CLAUDE = "work/claude-sonnet-4-6"


def _claude_tool_turn() -> AIMessage:
    """An Anthropic tool-calling turn: thinking, text and a tool_use block."""
    return AIMessage(
        content=[
            {"type": "thinking", "thinking": "plan", "signature": "ANT_SIG"},
            {"type": "text", "text": "Reading it."},
            {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"p": "a"}},
        ],
        tool_calls=[{"name": "read", "args": {"p": "a"}, "id": "toolu_1", "type": "tool_call"}],
        response_metadata={"stop_reason": "tool_use"},
        id="ai-1",
    )


class TheWriter(unittest.TestCase):
    def test_a_stamp_names_it(self) -> None:
        self.assertEqual(writer_of(stamped(AIMessage("hi"), GEMINI)), GEMINI)

    def test_stamping_keeps_the_providers_metadata(self) -> None:
        turn = stamped(_claude_tool_turn(), CLAUDE)
        self.assertEqual(turn.response_metadata["stop_reason"], "tool_use")
        self.assertEqual(turn.response_metadata[STAMP], CLAUDE)

    def test_a_turn_from_before_stamping_has_none(self) -> None:
        self.assertIsNone(writer_of(AIMessage("hi")))


class ThePortableForm(unittest.TestCase):
    def test_keeps_the_text_and_the_tool_calls(self) -> None:
        turn = portable(stamped(_claude_tool_turn(), CLAUDE))
        self.assertEqual(turn.content, "Reading it.")
        self.assertEqual([c["id"] for c in turn.tool_calls], ["toolu_1"])
        self.assertEqual(turn.id, "ai-1")

    def test_drops_everything_provider_specific(self) -> None:
        turn = portable(
            AIMessage(
                "answer",
                additional_kwargs={"__gemini_function_call_thought_signatures__": {"c": "SIG"}},
                response_metadata={STAMP: GEMINI, "model_provider": "google_genai"},
            )
        )
        self.assertEqual(turn.additional_kwargs, {})
        self.assertEqual(turn.response_metadata, {})

    def test_joins_text_from_mixed_content(self) -> None:
        turn = portable(AIMessage(["a", {"type": "text", "text": "b"}, {"type": "reasoning"}]))
        self.assertEqual(turn.content, "ab")


class ForAReader(unittest.TestCase):
    def test_its_own_turns_pass_unchanged_and_others_go_portable(self) -> None:
        own = stamped(
            AIMessage([{"type": "text", "text": "x", "extras": {"signature": "S"}}]), GEMINI
        )
        foreign = stamped(_claude_tool_turn(), CLAUDE)
        legacy = AIMessage([{"type": "text", "text": "old"}])
        human, tool = HumanMessage("q"), ToolMessage("r", tool_call_id="toolu_1")

        sent = for_reader([human, foreign, tool, own, legacy], GEMINI)

        self.assertIs(sent[0], human)
        self.assertEqual(sent[1].content, "Reading it.")
        self.assertIs(sent[2], tool)
        self.assertIs(sent[3], own)
        self.assertEqual(sent[4].content, "old")


if __name__ == "__main__":
    unittest.main()
