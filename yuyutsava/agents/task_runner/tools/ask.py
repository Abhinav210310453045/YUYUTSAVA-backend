"""TaskRunner tools: Asking the user a question mid-task.

Split out of the 609-line ``bind_tools`` (`R-RDB-02`); the factory in
``__init__`` assembles the families.
"""

from __future__ import annotations

import json
import logging

from langchain_core.tools import BaseTool, tool
from langgraph.types import interrupt

from yuyutsava.agents.task_runner.tools.support import (
    ToolContext,
)
from yuyutsava.run_context import current_context

_log = logging.getLogger("yuyutsava.agents.task_runner.tools.ask")


def make_ask_tools(ctx: ToolContext) -> list[BaseTool]:
    """Build the ask tools bound to one workspace."""
    agent_name = ctx.agent_name

    @tool
    async def tr_ask_user(
        question: str,
        options: list[str] | None = None,
    ) -> str:
        """Ask the user a question and return their text response.

        Use this when you need clarification, a choice between approaches, or
        explicit confirmation before an irreversible action. The question is
        shown to the user on their terminal and their answer is returned.

        Args:
            question: The question to show the user.
            options: Optional list of suggested responses shown as hints (not enforced).

        Returns:
            JSON: {status, result: {response: <user's answer>}}
        """

        ctx = current_context()
        parent_path = ctx.get("agent_path") or "orchestrator"
        # Override agent_path so the UI knows which subagent (not just "orchestrator")
        # is the asker. When agent_name is the default "agent" (no subagent), keep
        # the parent path unchanged.
        if agent_name and agent_name != "agent" and not parent_path.endswith(f"/{agent_name}"):
            ctx = {**ctx, "agent_path": f"{parent_path}/{agent_name}"}
        payload = {
            "type": "user_question",
            "question": question,
            "options": options or [],
            **ctx,
        }
        response: str = interrupt(payload)
        return json.dumps({"status": "success", "result": {"response": response}})

    return [
        tr_ask_user,
    ]
