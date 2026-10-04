"""A conversation can switch model per call without rebuilding the agent graph.

``llm/switch`` replaces ``request.model`` in ``awrap_model_call``
(docs/architecture/decisions/ADR-005-models-config.md §5; design §2 rules 3
and 8). That relies on three framework behaviours, each pinned here with real
graphs and fake models (nothing is sent):

1. **The model actually used is the swapped one.** The agent factory binds the
   tools to the *swapped* model on every call, not to the model it was built
   with.
2. **Prompt caching sees the swap.** deepagents puts the caller's middleware
   *outside* its Anthropic prompt-caching middleware, so the caching check sees
   the swapped model.
3. **The caching gate is a type check.** That gate is ``isinstance(model,
   ChatAnthropic)``. Claude through ``ChatAnthropicVertex`` or
   ``ChatBedrockConverse`` fails it, which is why ``policy/cache_control``
   exists. ``ChatAnthropicBedrock`` is a ``ChatAnthropic`` and passes it.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from pydantic import Field

anthropic_mw = pytest.importorskip("langchain_anthropic.middleware")


class _Recording(FakeMessagesListChatModel):
    """Answers from a script and records which tools were bound to it."""

    bound: list[list[str]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.bound.append(sorted(getattr(t, "name", str(t)) for t in tools))
        return self


class _SwapTo(AgentMiddleware):
    """The model switch in miniature: every call goes to *model*."""

    def __init__(self, model) -> None:
        super().__init__()
        self.model = model

    async def awrap_model_call(self, request, handler):
        return await handler(request.override(model=self.model))


@tool
def ping() -> str:
    """Answer pong."""
    return "pong"


def _scripted(text: str) -> _Recording:
    return _Recording(responses=[AIMessage(content=text)])


def _final_answer(runnable) -> str:
    """Drive *runnable* the way production does — asynchronously — and return its reply."""
    result = asyncio.run(runnable.ainvoke({"messages": [HumanMessage("hi")]}))
    return result["messages"][-1].content


def switched_agent_answer() -> tuple[str, list[list[str]], list[list[str]]]:
    """``(final answer, tools bound to the built model, tools bound to the swapped one)``."""
    built, swapped = _scripted("from the built model"), _scripted("from the swapped model")
    agent = create_agent(built, tools=[ping], middleware=[_SwapTo(swapped)])
    return _final_answer(agent), built.bound, swapped.bound


def test_a_swapped_model_answers_and_gets_the_tools() -> None:
    answer, built_bound, swapped_bound = switched_agent_answer()
    assert answer == "from the swapped model", f"the built model answered: {answer!r}"
    assert swapped_bound == [["ping"]], f"tools bound to the swapped model: {swapped_bound}"
    assert built_bound == [], f"the built model was still bound to tools: {built_bound}"


def model_seen_by_prompt_caching(middleware: list[AgentMiddleware]) -> str:
    """The answer text of the model deepagents' caching check was shown."""
    from deepagents import create_deep_agent

    seen: list[str] = []
    gate = anthropic_mw.AnthropicPromptCachingMiddleware._should_apply_caching

    def record(self, request: ModelRequest) -> bool:
        seen.append(request.model.responses[0].content)
        return gate(self, request)

    built = _scripted("built")
    with mock.patch.object(
        anthropic_mw.AnthropicPromptCachingMiddleware, "_should_apply_caching", record
    ):
        _final_answer(create_deep_agent(model=built, tools=[ping], middleware=middleware))
    if not seen:
        raise AssertionError("deepagents no longer runs AnthropicPromptCachingMiddleware")
    return seen[0]


def test_prompt_caching_sees_the_swapped_model() -> None:
    seen = model_seen_by_prompt_caching([_SwapTo(_scripted("swapped"))])
    assert seen == "swapped", (
        "deepagents' prompt caching now runs outside the caller's middleware and sees the "
        f"model the graph was built with ({seen!r}); llm/switch must move."
    )


def caching_applied(model) -> bool:
    """Whether the caching middleware marks a request to *model* for caching."""
    captured: list[ModelRequest] = []
    middleware = anthropic_mw.AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore")
    request = ModelRequest(model=model, messages=[HumanMessage("hi")])

    async def handler(r: ModelRequest) -> AIMessage:
        captured.append(r)
        return AIMessage(content="")

    asyncio.run(middleware.awrap_model_call(request, handler))
    return "cache_control" in captured[0].model_settings


def test_the_caching_gate_is_the_chat_anthropic_type() -> None:
    from langchain_anthropic import ChatAnthropic

    applied = {
        "ChatAnthropic": caching_applied(ChatAnthropic(api_key="unused", model="claude-sonnet-4-5"))
    }
    try:
        from google.auth.credentials import AnonymousCredentials
        from langchain_google_vertexai.model_garden import ChatAnthropicVertex

        vertex = ChatAnthropicVertex(
            model_name="claude-sonnet-4-5@20250929",
            project="p",
            location="global",
            credentials=AnonymousCredentials(),
        )
        applied["ChatAnthropicVertex"] = caching_applied(vertex)
    except ImportError:
        pass
    assert applied.pop("ChatAnthropic"), "caching no longer applies to ChatAnthropic itself"
    assert not any(applied.values()), (
        f"caching now applies to {[k for k, v in applied.items() if v]}; "
        "policy/cache_control may double-mark those requests"
    )


class TheChecksFail(unittest.TestCase):
    """Negative controls: each check can see the failure it guards against."""

    def test_without_a_swap_the_built_model_answers(self) -> None:
        built = _scripted("from the built model")
        self.assertEqual(_final_answer(create_agent(built, tools=[ping])), "from the built model")

    def test_without_a_swap_caching_sees_the_built_model(self) -> None:
        self.assertEqual(model_seen_by_prompt_caching([]), "built")

    def test_a_non_anthropic_model_is_not_cached(self) -> None:
        self.assertFalse(caching_applied(_scripted("x")))


if __name__ == "__main__":
    unittest.main()
