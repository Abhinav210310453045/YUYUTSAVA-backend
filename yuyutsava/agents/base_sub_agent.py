"""
BaseSubAgent — abstract base class for all future LLM sub-agents in YUYUTSAVA.

Every sub-agent that needs filesystem access must subclass this and:
  1. Set ``name``, ``description``, ``system_prompt`` as class attributes or properties.
  2. Optionally override ``extra_tools()`` to add domain-specific tools.
  3. Call ``build_react_agent(model, checkpointer)`` to get a runnable LangGraph graph.

The base class wires in the four TaskRunner tools automatically, so sub-agents
have a consistent, permission-gated filesystem interface without any boilerplate.

Example::

    class ResearchAgent(BaseSubAgent):
        name = "research-agent"
        description = "Reads external data files and writes analysis to workspace."
        system_prompt = "You are a research agent. Use tr_read_file for external data..."

        def extra_tools(self) -> list[BaseTool]:
            from my_tools import web_search
            return [web_search]

    agent_graph = ResearchAgent(task_runner).build_react_agent(model, checkpointer)
    result = await agent_graph.ainvoke({"messages": [HumanMessage("analyse /home/user/data.csv")]})
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent

from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.task_runner.tools import bind_tools


class BaseSubAgent(ABC):
    """
    Abstract base for all YUYUTSAVA LLM sub-agents.

    Provides automatic TaskRunner tool wiring and a consistent
    ``build_react_agent()`` / ``as_deepagents_subagent_spec()`` interface.
    """

    def __init__(self, task_runner: TaskRunnerAgent) -> None:
        self._task_runner = task_runner

    # ------------------------------------------------------------------
    # Required overrides
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique agent identifier, e.g. 'research-agent'."""

    @property
    @abstractmethod
    def description(self) -> str:
        """One-sentence description used by the parent agent when delegating."""

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """Full system instructions for this sub-agent's LLM."""

    # ------------------------------------------------------------------
    # Optional overrides
    # ------------------------------------------------------------------

    def extra_tools(self) -> list[BaseTool]:
        """
        Return agent-specific tools in addition to the TaskRunner tools.

        Override this in subclasses to add domain tools (web_search, etc.).
        """
        return []

    # ------------------------------------------------------------------
    # Provided by base class — do not override
    # ------------------------------------------------------------------

    def task_runner_tools(self) -> list[BaseTool]:
        """Return the four tr_* tools bound to this agent's TaskRunnerAgent."""
        return bind_tools(self._task_runner.workspace_root)

    def all_tools(self) -> list[BaseTool]:
        """Combined list: TaskRunner tools + any extra_tools()."""
        return self.task_runner_tools() + self.extra_tools()

    def build_react_agent(
        self,
        model: BaseChatModel,
        checkpointer: BaseCheckpointSaver,
    ) -> CompiledStateGraph:
        """
        Build a LangGraph react agent for this sub-agent.

        A checkpointer is **required** because the TaskRunner tools call
        ``interrupt()`` for HITL permission prompts, and LangGraph needs
        a checkpointer to persist state across interrupts.

        Args:
            model:        The LLM to use (ChatOpenAI or any BaseChatModel).
            checkpointer: A LangGraph checkpointer (e.g. MemorySaver()).

        Returns:
            A compiled LangGraph ``CompiledStateGraph`` ready for ``.ainvoke()``.
        """
        return create_react_agent(
            model=model,
            tools=self.all_tools(),
            prompt=self.system_prompt,
            checkpointer=checkpointer,
        )

    def as_deepagents_subagent_spec(self) -> dict[str, Any]:
        """
        Return a SubAgent spec dict compatible with ``create_deep_agent(subagents=...)``.

        This lets DeepAgent delegate to this sub-agent via its built-in ``task()`` tool
        without any additional wiring. The sub-agent receives TaskRunner tools + extra_tools.

        Usage::

            agent_spec = my_sub_agent.as_deepagents_subagent_spec()
            graph = create_deep_agent(model=model, backend=backend,
                                      subagents=[agent_spec], ...)
        """
        return {
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "tools": self.all_tools(),
        }
