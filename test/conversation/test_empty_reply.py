"""An empty reply is described before the repair removes it.

`R-BUG-23`. The empty replies that wedged the user's conversations could only be
guessed at: ``finish_reason`` was logged nowhere, the repair deleted the empty
message from the checkpoint, and the usage row counts visible output only. Now
the reply is read first and one line says why it was empty — "the model thought
through its whole output cap" (MAX_TOKENS) reads differently from a safety stop
or a genuine STOP with nothing to say.
"""

from __future__ import annotations

import asyncio
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from yuyutsava.conversation.recovery import EmptyReply, describe_empty_reply, recover_empty_turn


def _empty(finish: object, *, out: int = 0, thinking: int = 4096, inp: int = 97255) -> AIMessage:
    return AIMessage(
        content="",
        id="ai-empty",
        response_metadata={"finish_reason": finish},
        usage_metadata={
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": inp + out,
            "output_token_details": {"reasoning": thinking},
        },
    )


def _history(last: AIMessage) -> list:
    return [
        HumanMessage(content="plan my week", id="h1"),
        AIMessage(
            content="",
            id="ai-call",
            tool_calls=[{"id": "c1", "name": "tr_read_file", "args": {}, "type": "tool_call"}],
        ),
        ToolMessage(id="t1", tool_call_id="c1", name="tr_read_file", content="x" * 500),
        last,
    ]


class DescribingAnEmptyReply(unittest.TestCase):
    def test_the_finish_reason_and_the_token_split_are_read(self) -> None:
        facts = describe_empty_reply(_history(_empty("MAX_TOKENS")))
        self.assertEqual(
            facts,
            EmptyReply(
                finish_reason="MAX_TOKENS",
                input_tokens=97255,
                output_tokens=0,
                thinking_tokens=4096,
            ),
        )
        self.assertTrue(facts.ran_out_of_room)
        self.assertIn("MAX_TOKENS", facts.describe())
        self.assertIn("4,096 thinking", facts.describe())

    def test_sdk_spellings_of_the_reason_are_normalised(self) -> None:
        for raw in (
            "FinishReason.MAX_TOKENS",
            "max_tokens",
            "length",
            # A streamed reply: Vertex's chunk reasons, merged by concatenation.
            "FINISH_REASON_UNSPECIFIEDFINISH_REASON_UNSPECIFIEDMAX_TOKENS",
        ):
            with self.subTest(raw=raw):
                self.assertTrue(describe_empty_reply(_history(_empty(raw))).ran_out_of_room)
        self.assertFalse(describe_empty_reply(_history(_empty("STOP"))).ran_out_of_room)

    def test_a_reply_that_said_something_is_not_described(self) -> None:
        said = AIMessage(content="done", id="ai-ok")
        self.assertIsNone(describe_empty_reply(_history(said)))

    def test_missing_metadata_reads_as_unknown_not_an_error(self) -> None:
        facts = describe_empty_reply(_history(AIMessage(content="", id="ai-bare")))
        self.assertEqual(facts.finish_reason, "")
        self.assertFalse(facts.ran_out_of_room)


class _Agent:
    def __init__(self, messages: list) -> None:
        self.messages = messages

    async def aget_state(self, config):
        return type("S", (), {"values": {"messages": list(self.messages)}})()

    async def aupdate_state(self, config, values, as_node=None):
        return None


class TheRecoveryLogsIt(unittest.TestCase):
    def test_one_warning_line_names_the_cause_before_any_repair(self) -> None:
        async def drive(task, resume):
            return "answered", 1

        with self.assertLogs("yuyutsava.conversation.recovery", "WARNING") as logs:
            asyncio.run(
                recover_empty_turn(
                    _Agent(_history(_empty("MAX_TOKENS"))), "t1", lambda e: None, drive
                )
            )
        line = next(r for r in logs.output if "empty reply" in r)
        self.assertIn("thread=t1", line)
        self.assertIn("MAX_TOKENS", line)


if __name__ == "__main__":
    unittest.main()
