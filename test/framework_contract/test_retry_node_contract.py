"""The node the empty-turn retry writes as still leads straight to the model.

`conversation.recovery` removes an empty reply by writing the repair as the
``tools`` node (``aupdate_state(..., as_node=TOOLS_NODE)``), because the agent
graph's edge from that node goes to the model — so resuming runs the model again
on the repaired history. If deepagents renamed the node or re-routed it, the
first rung of the retry would stop working and every empty reply would fall
through to the nudge (`R-BUG-24`).
"""

from __future__ import annotations

import unittest

from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from yuyutsava.conversation.recovery import TOOLS_NODE


class _Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@tool
def noop(x: str) -> str:
    """Do nothing."""
    return x


def model_successors_of(node: str) -> list[str]:
    """Where the deepagents graph goes after *node*."""
    graph = create_deep_agent(model=_Model(responses=[AIMessage(content="")]), tools=[noop])
    drawn = graph.get_graph()
    if node not in drawn.nodes:
        raise AssertionError(f"the agent graph has no {node!r} node: {sorted(drawn.nodes)}")
    return sorted(e.target for e in drawn.edges if e.source == node)


def test_the_retry_node_leads_to_the_model() -> None:
    assert model_successors_of(TOOLS_NODE) == ["model"], (
        f"after {TOOLS_NODE!r} the graph goes to {model_successors_of(TOOLS_NODE)}, not the model"
    )


class TheCheckFails(unittest.TestCase):
    """Negative control: a node that does not exist is reported."""

    def test_a_renamed_node_is_named(self) -> None:
        with self.assertRaisesRegex(AssertionError, "no 'tool_node' node"):
            model_successors_of("tool_node")


if __name__ == "__main__":
    unittest.main()
