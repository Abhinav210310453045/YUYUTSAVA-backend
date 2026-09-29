"""A tool that raises gives the model an error result; the turn goes on (R-BUG-27).

LangGraph's tool node turns only argument-validation errors into a result, so
any other exception — Tavily rejecting a "site:"-only query, a store that is
down — ended the whole turn with a traceback and no answer. The policy adapter
wraps every tool call, and now hands such a failure back as an error result.
LangGraph's control flow still passes through it: an interrupt (a permission
ask) must pause the graph, not become an "error".

Driven through a real deepagents graph with a scripted model.
"""

from __future__ import annotations

import asyncio
import unittest

from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt

from yuyutsava.policy.adapter import LangChainPolicyAdapter
from yuyutsava.policy.base import Policy


class _Scripted(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@tool
def web_search(query: str) -> str:
    """Search the web."""
    raise RuntimeError("Query cannot contain only site: operators")


@tool
def delete_file(path: str) -> str:
    """Delete a file, after asking."""
    return f"deleted {path}" if interrupt({"ask": f"delete {path}?"}) == "yes" else "kept"


class _SeesResults(Policy):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list = []

    async def after_tool(self, call, result):
        self.seen.append(result)
        return result


def _call(name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"id": "c1", "name": name, "args": args, "type": "tool_call"}]
    )


def _run(tools, replies, policies=()):
    agent = create_deep_agent(
        model=_Scripted(responses=replies),
        tools=tools,
        middleware=[LangChainPolicyAdapter(list(policies))],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "t"}}

    async def go():
        await agent.ainvoke({"messages": [{"role": "user", "content": "go"}]}, config)
        return await agent.aget_state(config)

    return asyncio.run(asyncio.wait_for(go(), 30))


class ARaisingTool(unittest.TestCase):
    def test_the_model_gets_an_error_result_and_answers(self) -> None:
        seer = _SeesResults()
        with self.assertLogs("yuyutsava.policy.adapter", "WARNING") as logs:
            state = _run(
                [web_search],
                [
                    _call("web_search", {"query": "site:x.com"}),
                    AIMessage(content="Searched another way."),
                ],
                policies=[seer],
            )
        msgs = state.values["messages"]
        result = next(m for m in msgs if isinstance(m, ToolMessage))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.name, "web_search")
        self.assertIn("RuntimeError: Query cannot contain only site: operators", result.content)
        self.assertEqual(msgs[-1].content, "Searched another way.", "the turn went on")
        self.assertEqual(seer.seen[0].content, result.content, "after-tool policies see it")
        self.assertIn("tool web_search raised RuntimeError", logs.output[0])


class ControlFlowPassesThrough(unittest.TestCase):
    def test_an_interrupt_pauses_the_graph_instead_of_failing_the_call(self) -> None:
        state = _run(
            [delete_file],
            [_call("delete_file", {"path": "/tmp/x"}), AIMessage(content="done")],
        )
        self.assertTrue(state.interrupts, "the ask is pending, not turned into an error")
        self.assertFalse(
            [m for m in state.values["messages"] if isinstance(m, ToolMessage)],
            "no result was fabricated for the paused call",
        )


if __name__ == "__main__":
    unittest.main()
