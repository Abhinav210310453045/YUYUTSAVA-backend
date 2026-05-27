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

import fnmatch

from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.task_runner.tools import bind_tools
from yuyutsava.core.config import SearchConfig
from yuyutsava.core.tool_registry import ToolRegistry
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.skills.tools import make_read_skill_tool


class BaseSubAgent(ABC):
    """
    Abstract base for all YUYUTSAVA LLM sub-agents.

    Provides automatic TaskRunner tool wiring and a consistent
    ``build_react_agent()`` / ``as_deepagents_subagent_spec()`` interface.
    """

    # Class attribute. Subclasses set ``False`` to opt out of background mode.
    supports_async: bool = True

    def __init__(
        self,
        task_runner: TaskRunnerAgent,
        skill_registry: SkillRegistry | None = None,
        can_write_skills: bool = False,
        search_config: SearchConfig | None = None,
        mcp_manager: MCPClientManager | None = None,
        cap_enforcer: object | None = None,  # tools.search._CapEnforcer; untyped to avoid cycle
    ) -> None:
        self._task_runner = task_runner
        self._skill_registry = skill_registry
        self._can_write_skills = can_write_skills
        self._search_config = search_config
        self._mcp_manager = mcp_manager
        self._cap_enforcer = cap_enforcer

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

    def workspace_context_block(self) -> str:
        """Concrete WORKSPACE / SANDBOX / OUTPUT paths injected into the prompt.

        Subagents only see their static ``system_prompt``; the master's prompt
        (which carries the real paths) is invisible to them. Without this
        block, a subagent has no way to know where the sandbox actually lives
        and would hallucinate something like ``/sandbox`` when calling tr_*
        tools. Rendering the real paths from the task_runner ties the prompt
        to the live config rather than a guess.
        """
        ws = self._task_runner.workspace_root
        sb = self._task_runner.sandbox_root
        out = (ws / "_output").resolve()
        return (
            "## WORKSPACE CONTEXT\n"
            f"WORKSPACE_ROOT: {ws}\n"
            f"SANDBOX_ROOT:   {sb}\n"
            f"OUTPUT_DIR:     {out}\n"
            "All tr_* tools take REAL absolute paths. Pass paths under "
            "WORKSPACE_ROOT for workspace ops; under SANDBOX_ROOT for scratch "
            "work (tr_execute_in_sandbox cwd is SANDBOX_ROOT). Deliverables go "
            "under OUTPUT_DIR. Do NOT invent paths like /sandbox, /workspace, "
            "or /tmp — they will not exist.\n"
        )

    def rendered_system_prompt(self) -> str:
        """``system_prompt`` with the workspace-context block appended."""
        base = self.system_prompt.rstrip()
        return f"{base}\n\n{self.workspace_context_block()}"

    def task_runner_tools(self) -> list[BaseTool]:
        """Return the four tr_* tools bound to this agent's TaskRunnerAgent.

        ``self.name`` is threaded into each tool so HITL interrupts carry the
        subagent's identity in their ``agent_path`` (e.g. ``orchestrator/file-organizer``).
        """
        return bind_tools(self._task_runner.workspace_root, agent_name=self.name)

    def skill_tools(self) -> list[BaseTool]:
        """Return skill tools based on the can_write_skills flag.

        When can_write_skills=True (opt-in), returns both sk_read_skill and
        sk_write_skill. Default (False) returns read-only access.
        """
        if self._skill_registry is None:
            return []
        if self._can_write_skills:
            from yuyutsava.skills.tools import make_skill_tools
            return make_skill_tools(self._skill_registry)
        return [make_read_skill_tool(self._skill_registry)]

    def search_tools(self) -> list[BaseTool]:
        """Return ws_* tools required by this subagent's visible skills.

        The blanket attach from the MVP is gone: a subagent now only sees the
        ws_* tools that at least one of its visible skills declares via the
        ``requires_tools`` frontmatter list. ``file-organizer`` reads only
        ``pdf-to-archive``, which doesn't list any ws_* tools — so it gets
        zero. A future skill that does list one will pick it up automatically.
        """
        if self._search_config is None or self._skill_registry is None:
            return []
        wanted: set[str] = set()
        for skill in self._skill_registry.scan(agent=self.name):
            for pat in skill.requires_tools:
                wanted.add(pat)
        if not wanted:
            return []
        from yuyutsava.tools.search import make_search_tools
        all_tools = make_search_tools(self._search_config, cap_enforcer=self._cap_enforcer)
        return [t for t in all_tools if any(fnmatch.fnmatchcase(t.name, p) for p in wanted)]

    def mcp_tools(self) -> list[BaseTool]:
        """Return MCP tools scoped to this subagent's ``name``.

        Empty list if no manager was provided or if the config's ``scopes``
        map has no entry for this agent (and ``default_scope`` is empty).
        """
        if self._mcp_manager is None:
            return []
        return self._mcp_manager.tools_for(self.name)

    def all_tools(self) -> list[BaseTool]:
        """Combined list: TaskRunner + skill + search + MCP + extra_tools()."""
        return (
            self.task_runner_tools()
            + self.skill_tools()
            + self.search_tools()
            + self.mcp_tools()
            + self.extra_tools()
        )

    def build_tool_registry(self) -> ToolRegistry:
        """Build a ToolRegistry populated with all this agent's tools."""
        registry = ToolRegistry()
        registry.register_many(self.all_tools())
        return registry

    def build_react_agent(
        self,
        model: BaseChatModel,
        checkpointer: BaseCheckpointSaver | None,
    ) -> CompiledStateGraph:
        """
        Build a LangGraph react agent for this sub-agent.

        All tools are registered in the graph (so LangGraph can execute them).
        A ``tool_search`` tool is prepended so the agent can discover tool
        schemas on demand rather than having them all injected upfront.

        A checkpointer is **required** because the TaskRunner tools call
        ``interrupt()`` for HITL permission prompts, and LangGraph needs
        a checkpointer to persist state across interrupts.

        Args:
            model:        The LLM to use (ChatOpenAI or any BaseChatModel).
            checkpointer: A LangGraph checkpointer (e.g. MemorySaver()).

        Returns:
            A compiled LangGraph ``CompiledStateGraph`` ready for ``.ainvoke()``.
        """
        tool_registry = self.build_tool_registry()
        # tool_search is prepended so it appears first in the model's tool list.
        # All other tools follow — they're in the graph for execution, and their
        # schemas are served on demand via tool_search.
        tools_with_search = [tool_registry.make_tool_search_tool()] + self.all_tools()
        graph = create_react_agent(
            model=model,
            tools=tools_with_search,
            prompt=self.rendered_system_prompt(),
            checkpointer=checkpointer,
        )
        # Name the graph after the sub-agent so LangFuse traces show the real
        # agent name (e.g. "file-organizer") instead of a generic "LangGraph".
        graph.name = self.name
        return graph

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
            "system_prompt": self.rendered_system_prompt(),
            "tools": self.all_tools(),
        }

    # ------------------------------------------------------------------
    # Async (background) mode
    # ------------------------------------------------------------------

    def async_graph_id(self) -> str:
        """Stable graph_id used to register this subagent with the LangGraph host.

        Must be a Python-identifier-safe string after kebab→underscore conversion
        (see ``yuyutsava.async_subagents._lg_graphs``). Default: same as ``name``.
        """
        return self.name

    def async_subagent_name(self) -> str:
        """Name the master uses to invoke this subagent via ``start_async_task``.

        Suffix is load-bearing: ``AsyncSubAgentMiddleware`` rejects duplicate
        names, so the sync entry (``<name>``) and the async entry (``<name>-bg``)
        must differ.
        """
        return f"{self.name}-bg"

    def as_async_subagent_spec(self, url: str) -> dict[str, Any]:
        """Return an ``AsyncSubAgent`` TypedDict for ``create_deep_agent``.

        ``url`` is the base URL of an Agent Protocol server hosting this
        subagent's compiled graph (typically the in-process AsyncSubagentHost).
        Remote-hosted variants pass a different URL — same shape.
        """
        return {
            "name": self.async_subagent_name(),
            "description": f"[background] {self.description}",
            "graph_id": self.async_graph_id(),
            "url": url,
        }

    def build_async_graph(
        self,
        model: BaseChatModel,
        checkpointer: BaseCheckpointSaver,
    ) -> CompiledStateGraph:
        """Compiled graph registered with the LangGraph host.

        Default: same react graph as the sync path. Subclasses can override
        to compile a different graph for background execution (e.g. different
        prompt, longer recursion limit, different tool subset).

        No checkpointer is passed here: LangGraph API injects its own
        checkpointer at runtime, and embedding one causes a ValueError at
        graph load time.
        """
        return self.build_react_agent(model, None)
