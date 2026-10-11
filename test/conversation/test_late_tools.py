"""An MCP server that comes up after a chat opened: its tools work in that same chat.

The chat opens without waiting for its MCP servers. When one comes up, the
agent is rebuilt with its tools and swapped in at the start of the next turn —
never during one — so the conversation goes on, history intact, with the new
tools reachable the way every tool is: in the tool list, through tool_search,
and named in the prompt's connectors block. Real deepagents graphs, scripted
models, one shared checkpointer.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from test.support.agent_fingerprint import capture_cli
from yuyutsava.conversation.service import ConversationService
from yuyutsava.core.agent_stack import refresh_when_tools_change
from yuyutsava.mcp.status import CONNECTED, STARTING, ServerStatus, connectors_block


class _Scripted(FakeMessagesListChatModel):
    """Replies in order; binding tools is a no-op."""

    def bind_tools(self, tools, **kwargs):
        return self


@tool
def drive__search(query: str) -> str:
    """Search the user's Google Drive."""
    return f"found 2 files for {query!r}"


class _Manager:
    """An MCP manager whose one server comes up when told to."""

    def __init__(self) -> None:
        self.generation = 0
        self.up = False

    def come_up(self) -> None:
        self.up = True
        self.generation += 1

    def tools_for(self, _agent: str) -> list:
        return [drive__search] if self.up else []

    def connectors_block(self, _agent: str) -> str:
        state = (
            ServerStatus("drive", CONNECTED, tools=1)
            if self.up
            else ServerStatus("drive", STARTING)
        )
        return connectors_block([state], "~/.yuyutsava/mcp_config.json")


class TheSameChat(unittest.TestCase):
    def test_gets_the_tools_from_its_next_message_with_its_history(self) -> None:
        saver = InMemorySaver()
        before = create_deep_agent(
            model=_Scripted(responses=[AIMessage(content="drive is still starting")]),
            checkpointer=saver,
        )
        after = create_deep_agent(
            model=_Scripted(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "c1",
                                "name": "drive__search",
                                "args": {"query": "plan"},
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="I found 2 files."),
                ]
            ),
            tools=[drive__search],
            checkpointer=saver,
        )
        manager = _Manager()
        rebuilt: list[str] = []

        def rebuild():
            rebuilt.append("rebuilt")
            return after

        bundle = SimpleNamespace(
            agent=before, refresh_agent=refresh_when_tools_change(manager, rebuild)
        )
        convo = ConversationService(
            store=None,
            session=SimpleNamespace(id="s1", thread_id="t-late"),
            workspace=Path("."),
            bundle=bundle,
            bookkeep=False,
        )
        events: list = []

        async def go():
            first = await convo.run_turn("search my drive for the plan", on_event=events.append)
            manager.come_up()  # between messages, the server comes up
            second = await convo.run_turn("try again now", on_event=events.append)
            third = await convo.run_turn("thanks", on_event=events.append)
            state = await after.aget_state({"configurable": {"thread_id": "t-late"}})
            return first, second, state.values["messages"], third

        first, second, messages, _ = asyncio.run(go())
        self.assertIn("still starting", first)
        self.assertEqual(second, "I found 2 files.")
        self.assertIs(bundle.agent, after)
        self.assertEqual(rebuilt, ["rebuilt"])  # once, on the change — not every turn
        said = [m.content for m in messages if isinstance(m.content, str)]
        self.assertIn("search my drive for the plan", said)  # the first turn is still there
        self.assertTrue(any("found 2 files for 'plan'" in text for text in said))


class TheRebuiltAgent(unittest.TestCase):
    """Built again with the same sandbox, it reaches the new tools every way a tool is reached."""

    def test_has_the_tools_in_its_list_and_its_prompt_and_keeps_its_sandbox(self) -> None:
        manager = _Manager()
        first, built = capture_cli(
            mcp_tools=manager.tools_for("cli"), mcp_connectors=manager.connectors_block("cli")
        )
        self.assertIn("drive: still starting", built["system_prompt"])
        self.assertNotIn("drive__search", [t.name for t in built["tools"]])

        manager.come_up()
        second, rebuilt = capture_cli(
            mcp_tools=manager.tools_for("cli"),
            mcp_connectors=manager.connectors_block("cli"),
            sandbox=first.sandbox,
        )
        self.assertIn("drive__search", [t.name for t in rebuilt["tools"]])
        self.assertIn("drive: connected, 1 tools", rebuilt["system_prompt"])
        self.assertIs(second.sandbox, first.sandbox)


if __name__ == "__main__":
    unittest.main()
