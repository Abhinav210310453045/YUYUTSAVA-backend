"""``ChatGoogleGenerativeAI`` still does, on Vertex, what Gemini on Vertex relies on.

Gemini on Vertex is built with ``ChatGoogleGenerativeAI(vertexai=True)`` because
``ChatVertexAI`` is deprecated and does not do the following three things
(docs/architecture/decisions/ADR-005-models-config.md; design §2 rule 14):

- **Retry on stream open.** A 429 or 503 that arrives when a stream opens is
  retried, up to ``max_retries`` attempts. Over gRPC, ``ChatVertexAI`` saw that
  status only on the first read, outside every retry layer.
- **Gemini 3 thought signatures.** A function call in the active loop that has
  no thought signature (portable history, or a turn from before an upgrade)
  gets the SDK's placeholder. Gemini 3 rejects the request without one.
- **JSON Schema type lists.** A tool schema that uses one, as MCP servers
  publish them, converts instead of failing the request.

All three are checked with the installed SDK and an ``httpx.MockTransport``.
Nothing is sent.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import unittest

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

genai = pytest.importorskip("langchain_google_genai")
httpx = pytest.importorskip("httpx")
oauth2 = pytest.importorskip("google.oauth2.credentials")

_CHUNK = {
    "candidates": [
        {"content": {"role": "model", "parts": [{"text": "hello"}]}, "finishReason": "STOP"}
    ],
    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
}


class _StaticToken(oauth2.Credentials):
    """A token valid for an hour, so no auth server is ever asked for one."""

    def __init__(self) -> None:
        expiry = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + datetime.timedelta(
            hours=1
        )
        super().__init__(token="unused", expiry=expiry)

    def refresh(self, request: object) -> None:
        raise AssertionError("the static token was asked to refresh")


def _vertex_model(handler: object, *, model: str = "gemini-2.5-flash", max_retries: int = 3):
    transport = {"transport": httpx.MockTransport(handler)}
    return genai.ChatGoogleGenerativeAI(
        model=model,
        vertexai=True,
        project="p",
        location="us-central1",
        credentials=_StaticToken(),
        max_retries=max_retries,
        client_args=transport,  # genai builds its sync and async clients from these
    )


def stream_attempts(*, fail_first: int, status: int, max_retries: int) -> tuple[int, str]:
    """``(requests made, outcome)`` for a stream whose first *fail_first* opens fail."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) <= fail_first:
            return httpx.Response(status, json={"error": {"code": status, "message": "busy"}})
        body = f"data: {json.dumps(_CHUNK)}\r\n\r\n".encode()
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async def run() -> str:
        text = ""
        async for chunk in _vertex_model(handler, max_retries=max_retries).astream("hi"):
            text += chunk.text
        return text

    try:
        outcome = asyncio.run(run())
    except Exception as exc:  # the outcome under test
        outcome = type(exc).__name__
    return len(calls), outcome


def test_a_stream_that_opens_busy_is_retried_on_vertex() -> None:
    for status in (429, 503):
        attempts, outcome = stream_attempts(fail_first=1, status=status, max_retries=3)
        assert (attempts, outcome) == (2, "hello"), (
            f"a {status} on stream open was not retried on Vertex: {attempts} request(s), "
            f"outcome {outcome!r}. llm/quirks/first_chunk_retry must cover this path again."
        )


def signed_function_calls(model: str) -> list[bool]:
    """Whether each function call in the request carries a thought signature."""
    call = AIMessage(
        content="", tool_calls=[{"name": "read_file", "args": {"path": "a"}, "id": "c1"}]
    )
    messages = [HumanMessage("read a"), call, ToolMessage("hello", tool_call_id="c1")]
    request = _vertex_model(lambda r: httpx.Response(500), model=model)._prepare_request(messages)
    return [
        bool(part.thought_signature)
        for content in request["contents"]
        for part in content.parts or []
        if part.function_call
    ]


def test_gemini_3_function_calls_without_a_signature_get_one() -> None:
    assert signed_function_calls("gemini-3.5-flash") == [True], (
        "an unsigned function call in Gemini 3's active loop is no longer given a placeholder "
        "signature; Gemini 3 rejects that request. llm/quirks/gemini_parts must add it."
    )


def test_a_json_schema_type_list_converts() -> None:
    from langchain_google_genai._function_utils import convert_to_genai_function_declarations

    schema = {"type": "object", "properties": {"account_id": {"type": ["string", "null"]}}}
    tool = {
        "type": "function",
        "function": {"name": "manage", "description": "d", "parameters": schema},
    }
    declaration = convert_to_genai_function_declarations([tool])[0].function_declarations[0]
    assert declaration.parameters.properties["account_id"].type == "STRING"


class TheChecksFail(unittest.TestCase):
    """Negative controls: each check can see the failure it guards against."""

    def test_no_retry_budget_means_the_busy_open_is_seen(self) -> None:
        self.assertEqual(
            stream_attempts(fail_first=1, status=429, max_retries=1), (1, "ClientError")
        )

    def test_a_pre_gemini_3_model_gets_no_placeholder(self) -> None:
        self.assertEqual(signed_function_calls("gemini-2.5-flash"), [False])


if __name__ == "__main__":
    unittest.main()
