"""Model Garden on Vertex: Claude and the open models are reachable the way we build them.

The ``vertex`` provider type serves three families
(docs/architecture/decisions/ADR-005-models-config.md §3; design §2 rules 6
and 13):

- **Gemini** goes through ``ChatGoogleGenerativeAI``. See
  ``test_genai_vertex_contract``.
- **Claude** goes through ``ChatAnthropicVertex``, which must accept a
  credentials object and reach the ``global`` endpoint. Claude is not served
  in every region.
- **Open models (MaaS)** go through ``ChatOpenAI`` at Vertex's OpenAI-compatible
  endpoint. LangChain's own MaaS classes accept a fixed list of models, build an
  invalid host for ``global``, and keep a token fetched once at construction
  until it expires.

  Our route passes a *sync* token function as ``api_key``. ``ChatOpenAI`` calls
  it on every request, and for async calls it runs it in an executor thread. So
  a Google token refresh, which is blocking I/O, never runs on the event loop.

Nothing is sent: requests are answered by an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest

import pytest

httpx = pytest.importorskip("httpx")
openai_chat = pytest.importorskip("langchain_openai")

_MAAS_MODEL = "meta/llama-4-maverick-17b-128e-instruct-maas"
_MAAS_BASE = "https://aiplatform.googleapis.com/v1/projects/p/locations/global/endpoints/openapi"
_COMPLETION = {
    "id": "x",
    "object": "chat.completion",
    "created": 0,
    "model": _MAAS_MODEL,
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def maas_requests(
    token_source, *, async_token: bool = False
) -> tuple[list[tuple[str, str, str]], list[bool]]:
    """Two async calls, then one sync call, through a MaaS ``ChatOpenAI``.

    Returns ``(requests as (url, Authorization, model), for each token fetch
    whether it ran on the event loop's thread)``.
    """
    seen: list[tuple[str, str, str]] = []
    fetch_threads: list[int] = []
    loop_thread: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append((str(request.url), request.headers.get("authorization", ""), model))
        return httpx.Response(200, json=_COMPLETION)

    def token() -> str:
        fetch_threads.append(threading.get_ident())
        return token_source()

    async def token_on_the_loop() -> str:
        return token()

    transport = httpx.MockTransport(handler)
    model = openai_chat.ChatOpenAI(
        model=_MAAS_MODEL,
        base_url=_MAAS_BASE,
        api_key=token_on_the_loop if async_token else token,
        http_client=httpx.Client(transport=transport),
        http_async_client=httpx.AsyncClient(transport=transport),
    )

    async def two_async_calls() -> None:
        loop_thread.append(threading.get_ident())
        await model.ainvoke("a")
        await model.ainvoke("b")

    asyncio.run(two_async_calls())
    if not async_token:  # an async-only key cannot serve a sync call
        model.invoke("c")
    return seen, [thread in loop_thread for thread in fetch_threads]


def test_maas_calls_fetch_a_token_per_request_off_the_event_loop() -> None:
    tokens = iter(["tok-1", "tok-2", "tok-3"])
    seen, on_loop = maas_requests(lambda: next(tokens))
    url = f"{_MAAS_BASE}/chat/completions"
    assert seen == [
        (url, "Bearer tok-1", _MAAS_MODEL),
        (url, "Bearer tok-2", _MAAS_MODEL),
        (url, "Bearer tok-3", _MAAS_MODEL),
    ], f"MaaS requests went out as {seen}"
    assert on_loop[:2] == [False, False], "an async call fetched its token on the event loop"


def test_claude_on_vertex_reaches_the_global_endpoint_with_our_credentials() -> None:
    model_garden = pytest.importorskip("langchain_google_vertexai.model_garden")
    from google.auth.credentials import AnonymousCredentials

    credentials = AnonymousCredentials()
    model = model_garden.ChatAnthropicVertex(
        model_name="claude-sonnet-4-5@20250929",
        project="p",
        location="global",
        credentials=credentials,
    )
    assert str(model.client.base_url) == "https://aiplatform.googleapis.com/v1/"
    assert model.credentials is credentials


class TheChecksFail(unittest.TestCase):
    """Negative controls: each check can see the failure it guards against."""

    def test_a_fixed_token_is_reused(self) -> None:
        seen, _ = maas_requests(lambda: "fixed")
        self.assertEqual({auth for _, auth, _ in seen}, {"Bearer fixed"})

    def test_an_async_token_function_runs_on_the_loop(self) -> None:
        _, on_loop = maas_requests(lambda: "t", async_token=True)
        self.assertEqual(on_loop, [True, True])


if __name__ == "__main__":
    unittest.main()
