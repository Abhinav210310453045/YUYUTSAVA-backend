"""Claude on Vertex and on Bedrock caches its prompt when asked the way ChatAnthropic is.

``llm/prompt_cache`` sets LangChain's ``cache_control`` call setting for models
that take it (design rule 8), because deepagents' own caching only fires for
``ChatAnthropic``. That relies on each SDK still turning the setting into
Anthropic's cache marks:

* ``ChatAnthropicVertex`` — on the last block of the last message;
* ``ChatBedrockConverse`` — as ``cachePoint`` blocks after the system prompt and
  the last message.

Payloads are built and captured locally; nothing is sent.
"""

from __future__ import annotations

import asyncio
import json
import unittest
import warnings
from unittest import mock

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from test.support.targets import every_type_target
from yuyutsava.llm.factory import chat_model, supports
from yuyutsava.llm.handle import Capability
from yuyutsava.llm.prompt_cache import CACHE_CONTROL, PromptCacheMiddleware

MESSAGES = [SystemMessage("You are helpful."), HumanMessage("hi")]


def _model(reference: str):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return chat_model(every_type_target(reference))


def vertex_claude_payload(**settings) -> dict:
    """What ``ChatAnthropicVertex`` would send for MESSAGES with *settings*."""
    pytest.importorskip("langchain_google_vertexai")
    return _model("vertex/claude-sonnet-4-6")._format_params(messages=MESSAGES, **settings)


def bedrock_claude_request(**settings) -> dict:
    """The ``converse`` request ``ChatBedrockConverse`` makes for MESSAGES with *settings*."""
    pytest.importorskip("langchain_aws")
    model = _model("bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    client = mock.MagicMock()
    client.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
    }
    model.client = client
    model.invoke(MESSAGES, **settings)
    return client.converse.call_args.kwargs


def test_vertex_claude_marks_the_last_block() -> None:
    payload = vertex_claude_payload(cache_control=CACHE_CONTROL)
    last = payload["messages"][-1]["content"][-1]
    assert last.get("cache_control") == CACHE_CONTROL, payload["messages"]
    assert "cache_control" not in payload, "the setting leaked to the top level"


def test_bedrock_claude_adds_cache_points() -> None:
    request = json.dumps(bedrock_claude_request(cache_control=CACHE_CONTROL))
    assert request.count('"cachePoint"') >= 2, request


def test_only_claude_outside_chat_anthropic_takes_the_setting() -> None:
    takes = {
        ref: supports(_model(ref), Capability.CACHE_CONTROL_KWARG)
        for ref in (
            "vertex/claude-sonnet-4-6",
            "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "vertex/gemini-2.5-flash",
            "vertex/openai/gpt-oss-120b-maas",
            "bedrock/meta.llama3-70b-instruct-v1:0",
            "anthropic/claude-haiku-4-5-20251001",
        )
    }
    assert takes == {
        "vertex/claude-sonnet-4-6": True,
        "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0": True,
        "vertex/gemini-2.5-flash": False,
        "vertex/openai/gpt-oss-120b-maas": False,
        "bedrock/meta.llama3-70b-instruct-v1:0": False,
        "anthropic/claude-haiku-4-5-20251001": False,  # deepagents' own caching covers it
    }, takes


def settings_sent_to(reference: str) -> dict:
    """The model settings ``PromptCacheMiddleware`` hands on for a call to *reference*."""
    from langchain.agents.middleware import ModelRequest

    seen: list = []

    async def handler(request):
        seen.append(request.model_settings)
        return None

    request = ModelRequest(model=_model(reference), messages=[HumanMessage("hi")])
    asyncio.run(PromptCacheMiddleware().awrap_model_call(request, handler))
    return seen[0]


def test_the_middleware_asks_only_for_those_models() -> None:
    assert settings_sent_to("vertex/claude-sonnet-4-6") == {"cache_control": CACHE_CONTROL}
    assert settings_sent_to("vertex/gemini-2.5-flash") == {}


class TheChecksFail(unittest.TestCase):
    """Negative controls: without the setting, neither SDK marks anything."""

    def test_vertex_without_the_setting(self) -> None:
        payload = vertex_claude_payload()
        self.assertNotIn("cache_control", json.dumps(payload["messages"]))

    def test_bedrock_without_the_setting(self) -> None:
        self.assertNotIn('"cachePoint"', json.dumps(bedrock_claude_request()))


if __name__ == "__main__":
    unittest.main()
