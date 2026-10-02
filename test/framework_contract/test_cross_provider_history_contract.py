"""Every chat SDK still accepts a portable history written by any other one.

A conversation that switches model carries the previous model's assistant turns.
Sent as checkpointed, they break the next model:

- Anthropic history raises in the Vertex Gemini and Bedrock request builders.
- Gemini history hands Anthropic a ``thinking`` block with no signature.
- Gemini's ``thought_signature`` rides into Mistral's text parts.

LangChain's standard ``v1`` content blocks are not universal either. Only the
*portable* form — a turn's text and its ``tool_calls`` — is accepted by every
builder. So that is what goes to any model other than the one that wrote a turn
(docs/architecture/decisions/ADR-005-models-config.md §5;
docs/design/model-config.md §2a).

How the pairs are built:

- **Messages:** each producer uses its own SDK's response parser, so they are
  the messages a checkpoint holds.
- **Payloads:** each target uses its own SDK's request builder, so the payload is
  the one that would be sent.

Producers include ``ChatVertexAI``, because existing checkpoints were written by it. Gemini on
Vertex is now *built* with ``ChatGoogleGenerativeAI(vertexai=True)`` (the ``genai_vertex``
target), so the pair that matters most for that move — old Vertex history into the new
builder — is one of the pairs checked.

Nothing is sent. A pair whose SDK is not installed is skipped.

The selection stamp rides on ``response_metadata``. When a stream is merged,
custom strings there are concatenated chunk by chunk, so the stamp is set on the
first chunk only. :func:`test_a_first_chunk_stamp_survives_stream_merging` pins
that this yields exactly one value.
"""

from __future__ import annotations

import base64
import json
import unittest
from collections.abc import Callable

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)

#: The stamp ``llm/quirks/history_portable`` reads to tell who wrote a turn.
STAMP = "yuyutsava_model"

#: Planted in every producer's provider-specific fields. Any of them in a
#: payload means a foreign field crossed over.
_SENTINELS = ("VTX_SIG", "GEN_SIG", "ANT_SIG", "ANT_REDACTED", "BR_SIG", "FOREIGN_THOUGHT")
_FOREIGN_KEYS = ('"thought_signature"', '"signature"', '"redacted_thinking"', '"reasoning_content"')

Turn = tuple[AIMessage, AIMessage]  # (tool-calling turn, final answer)


# ─── producers: what each SDK's parser puts in a checkpoint ───────────────


def _vertex_gemini() -> Turn:
    from google.cloud.aiplatform_v1beta1.types import Candidate, Content, FunctionCall, Part
    from langchain_google_vertexai.chat_models import _parse_response_candidate as parse

    def turn(*parts: Part) -> AIMessage:
        return parse(Candidate(content=Content(role="model", parts=list(parts))))

    call = turn(
        Part(text="FOREIGN_THOUGHT", thought=True),
        Part(text="I'll read it.", thought_signature=b"VTX_SIG_TEXT"),
        Part(
            function_call=FunctionCall(name="read_file", args={"path": "a.txt"}),
            thought_signature=b"VTX_SIG_CALL",
        ),
    )
    return call, turn(Part(text="It says hello.", thought_signature=b"VTX_SIG_FINAL"))


def _genai_gemini() -> Turn:
    from google.genai import types as t
    from langchain_google_genai.chat_models import _parse_response_candidate as parse

    def turn(*parts: t.Part) -> AIMessage:
        candidate = t.Candidate(content=t.Content(role="model", parts=list(parts)))
        return parse(candidate, model_name="gemini-3-flash")

    call = turn(
        t.Part(text="FOREIGN_THOUGHT", thought=True),
        t.Part(text="I'll read it.", thought_signature=b"GEN_SIG_TEXT"),
        t.Part(
            function_call=t.FunctionCall(name="read_file", args={"path": "a.txt"}),
            thought_signature=b"GEN_SIG_CALL",
        ),
    )
    return call, turn(t.Part(text="It says hello.", thought_signature=b"GEN_SIG_FINAL"))


def _anthropic() -> Turn:
    import anthropic.types as at
    from langchain_anthropic import ChatAnthropic

    model = ChatAnthropic(api_key="unused", model="claude-sonnet-4-5")

    def turn(content: list, stop: str) -> AIMessage:
        message = at.Message(
            id="msg_1", type="message", role="assistant", model="claude-sonnet-4-5",
            content=content, stop_reason=stop, stop_sequence=None,
            usage=at.Usage(input_tokens=1, output_tokens=1),
        )  # fmt: skip
        return model._format_output(message).generations[0].message

    call = turn(
        [
            at.ThinkingBlock(type="thinking", thinking="FOREIGN_THOUGHT", signature="ANT_SIG"),
            at.RedactedThinkingBlock(type="redacted_thinking", data="ANT_REDACTED"),
            at.TextBlock(type="text", text="I'll read it.", citations=None),
            at.ToolUseBlock(
                type="tool_use", id="toolu_01", name="read_file", input={"path": "a.txt"}
            ),
        ],
        "tool_use",
    )
    return call, turn(
        [at.TextBlock(type="text", text="It says hello.", citations=None)], "end_turn"
    )


def _openai_compatible() -> Turn:
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(api_key="unused", model="any", base_url="http://127.0.0.1:9/v1")

    def turn(message: dict, finish: str) -> AIMessage:
        response = {
            "id": "c1", "object": "chat.completion", "created": 0, "model": "any",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }  # fmt: skip
        return model._create_chat_result(response).generations[0].message

    call_dict = {"id": "call_01", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}  # fmt: skip
    call = turn(
        {"role": "assistant", "content": "I'll read it.", "tool_calls": [call_dict]}, "tool_calls"
    )
    return call, turn({"role": "assistant", "content": "It says hello."}, "stop")


def _bedrock() -> Turn:
    from langchain_aws.chat_models.bedrock_converse import _parse_response as parse

    def turn(content: list, stop: str) -> AIMessage:
        return parse({
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": stop,
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
            "metrics": {"latencyMs": 1},
        })  # fmt: skip

    reasoning = {"reasoningText": {"text": "FOREIGN_THOUGHT", "signature": "BR_SIG"}}
    call = turn(
        [
            {"reasoningContent": reasoning},
            {"text": "I'll read it."},
            {
                "toolUse": {
                    "toolUseId": "tooluse_01",
                    "name": "read_file",
                    "input": {"path": "a.txt"},
                }
            },
        ],
        "tool_use",
    )
    return call, turn([{"text": "It says hello."}], "end_turn")


PRODUCERS: dict[str, Callable[[], Turn]] = {
    "vertex_gemini": _vertex_gemini,
    "genai_gemini": _genai_gemini,
    "anthropic": _anthropic,
    "openai_compatible": _openai_compatible,
    "bedrock": _bedrock,
}


# ─── targets: the payload each SDK would send ─────────────────────────────


def _to_openai_compatible(messages: list[BaseMessage]) -> object:
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(api_key="unused", model="any", base_url="http://127.0.0.1:9/v1")
    return model._get_request_payload(messages)


def _to_anthropic(messages: list[BaseMessage]) -> object:
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(api_key="unused", model="claude-sonnet-4-5")._get_request_payload(messages)


def _to_anthropic_thinking(messages: list[BaseMessage]) -> object:
    from langchain_anthropic import ChatAnthropic

    model = ChatAnthropic(
        api_key="unused", model="claude-sonnet-4-5", max_tokens=16_000,
        thinking={"type": "enabled", "budget_tokens": 4_000},
    )  # fmt: skip
    return model._get_request_payload(messages)


def _to_anthropic_vertex(messages: list[BaseMessage]) -> object:
    from google.auth.credentials import AnonymousCredentials
    from langchain_google_vertexai.model_garden import ChatAnthropicVertex

    model = ChatAnthropicVertex(
        model_name="claude-sonnet-4-5@20250929", project="p", location="global",
        credentials=AnonymousCredentials(),
    )  # fmt: skip
    return model._format_params(messages=messages)


def _to_genai_vertex(messages: list[BaseMessage]) -> object:
    """Gemini on Vertex as we build it: ``ChatGoogleGenerativeAI(vertexai=True)``."""
    from google.auth.credentials import AnonymousCredentials
    from langchain_google_genai import ChatGoogleGenerativeAI

    model = ChatGoogleGenerativeAI(
        model="gemini-3-flash",
        vertexai=True,
        project="p",
        location="us-central1",
        credentials=AnonymousCredentials(),
    )
    return model._prepare_request(messages)


def _to_genai_gemini(messages: list[BaseMessage]) -> object:
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(model="gemini-3-flash", api_key="unused")._prepare_request(
        messages
    )


def _to_bedrock(messages: list[BaseMessage]) -> object:
    from langchain_aws.chat_models.bedrock_converse import _messages_to_bedrock

    return _messages_to_bedrock(messages)


def _to_mistral(messages: list[BaseMessage]) -> object:
    from langchain_mistralai import ChatMistralAI

    return ChatMistralAI(api_key="unused", model="mistral-large-latest")._create_message_dicts(
        messages, None
    )


def _to_cohere(messages: list[BaseMessage]) -> object:
    from langchain_cohere.chat_models import get_cohere_chat_request_v2

    return get_cohere_chat_request_v2(messages)


TARGETS: dict[str, Callable[[list[BaseMessage]], object]] = {
    "openai_compatible": _to_openai_compatible,
    "anthropic": _to_anthropic,
    "anthropic+thinking": _to_anthropic_thinking,
    "anthropic_vertex": _to_anthropic_vertex,
    "genai_vertex": _to_genai_vertex,
    "genai_gemini": _to_genai_gemini,
    "bedrock": _to_bedrock,
    "mistral": _to_mistral,
    "cohere": _to_cohere,
}


# ─── the check ────────────────────────────────────────────────────────────


def portable(message: AIMessage) -> AIMessage:
    """The turn's text and tool calls — the form every builder accepts."""
    if isinstance(message.content, str):
        text = message.content
    else:
        text = "".join(
            block if isinstance(block, str) else block.get("text", "")
            for block in message.content
            if isinstance(block, str) or block.get("type") == "text"
        )
    return AIMessage(content=text, tool_calls=message.tool_calls, id=message.id)


def conversation(turn: Turn, shape: Callable[[AIMessage], AIMessage]) -> list[BaseMessage]:
    """Ask, call a tool, get its result, answer, ask again."""
    call, final = turn
    return [
        HumanMessage("read a.txt"),
        shape(call),
        ToolMessage("hello", tool_call_id=call.tool_calls[0]["id"]),
        shape(final),
        HumanMessage("and now?"),
    ]


def foreign_fields(payload: object) -> list[str]:
    """Provider-specific fields from another SDK that reached *payload*."""
    text = json.dumps(payload, default=repr)
    encoded = [base64.b64encode(s.encode()).decode().rstrip("=")[:8] for s in _SENTINELS]
    planted = [s for s, e in zip(_SENTINELS, encoded, strict=True) if s in text or e in text]
    return planted + [key for key in _FOREIGN_KEYS if key in text]


def portable_history_failures() -> tuple[list[str], int]:
    """``(failures, pairs checked)`` for portable history across every installed pair."""
    failures: list[str] = []
    checked = 0
    for source, produce in PRODUCERS.items():
        try:
            turn = produce()
        except ImportError:
            continue
        for target, build in TARGETS.items():
            if target.split("+")[0] == source:
                continue  # a model's own history goes to it unchanged
            try:
                payload = build(conversation(turn, portable))
            except ImportError:
                continue
            except Exception as exc:  # the finding *is* that the builder raised
                failures.append(f"{source} -> {target}: raised {type(exc).__name__}: {exc}")
                checked += 1
                continue
            checked += 1
            if leaked := foreign_fields(payload):
                failures.append(f"{source} -> {target}: carried {leaked}")
    return failures, checked


def test_portable_history_is_accepted_by_every_builder() -> None:
    failures, checked = portable_history_failures()
    if not checked:
        pytest.skip("no chat SDK pair installed")
    assert not failures, (
        "portable history no longer crosses providers cleanly — "
        "llm/quirks/history_portable must change with it:\n  " + "\n  ".join(failures)
    )


def test_a_first_chunk_stamp_survives_stream_merging() -> None:
    first = AIMessageChunk(content="a", response_metadata={STAMP: "work/gemini-3.5-flash"})
    merged = first + AIMessageChunk(content="b") + AIMessageChunk(content="c")
    assert merged.response_metadata.get(STAMP) == "work/gemini-3.5-flash", (
        f"merging stream chunks changed the stamp to {merged.response_metadata.get(STAMP)!r}"
    )


class TheCheckFails(unittest.TestCase):
    """Negative controls: the scan sees a foreign field, and raw history has some."""

    def test_a_planted_signature_is_named(self) -> None:
        self.assertEqual(
            foreign_fields({"parts": [{"thought_signature": "x"}]}), ['"thought_signature"']
        )

    def test_a_base64_encoded_sentinel_is_named(self) -> None:
        payload = {"text": base64.b64encode(b"VTX_SIG_FINAL").decode()}
        self.assertIn("VTX_SIG", foreign_fields(payload))

    def test_raw_anthropic_history_reaches_an_openai_payload(self) -> None:
        try:
            turn = _anthropic()
            payload = _to_openai_compatible(conversation(turn, lambda m: m))
        except ImportError:
            self.skipTest("langchain-anthropic or langchain-openai not installed")
        self.assertIn('"redacted_thinking"', foreign_fields(payload))


if __name__ == "__main__":
    unittest.main()
