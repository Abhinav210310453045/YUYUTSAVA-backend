"""
Orchestrator agent: ``create_deep_agent`` master with registered subagents.

The master uses deepagent's built-in ``task(subagent_type, description)``
tool to delegate to specialised subagents. Each task gets a fresh thread_id;
nothing accumulates across events.

Subagents are registered at build time via ``BaseSubAgent.as_deepagents_subagent_spec()``.
Subagent interrupts (tr_* permission prompts) bubble up through the master
graph and are routed to the user via the daemon's channel router (Full HITL).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph

from yuyutsava.agents.base_sub_agent import BaseSubAgent
from yuyutsava.agents.orchestrator.capabilities import render_capabilities_block
from yuyutsava.agents.orchestrator.prompts import render_system_prompt
from yuyutsava.daemon.budget import BudgetMiddleware
from yuyutsava.daemon.channels import AskPrompt, ChannelRouter
from yuyutsava.core.tool_filter_middleware import ToolFilterMiddleware
from yuyutsava.events.store import Store
from yuyutsava.core.config import SearchConfig
from yuyutsava.events.tools import make_recall_tool
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.skills.tools import make_skill_tools
from yuyutsava.tools.search import make_search_tools

logger = logging.getLogger("yuyutsava.agents.orchestrator")


@dataclass
class OrchestratorDeps:
    """Bag of dependencies the orchestrator and its tools need at call time."""

    subagents: dict[str, BaseSubAgent]
    subagent_model: BaseChatModel
    channels: ChannelRouter
    store: Store
    subagent_token_budget: int
    skill_registry: SkillRegistry | None = None
    workspace_root: Path | None = None
    mcp_manager: MCPClientManager | None = None
    search_config: SearchConfig | None = None
    cap_enforcer: object | None = None  # tools.search._CapEnforcer


def build_orchestrator(
    *,
    model: BaseChatModel,
    deps: OrchestratorDeps,
    budget_tokens: int,
    skill_registry: SkillRegistry | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    prefs_block: str = "",
) -> CompiledStateGraph:
    """Build a fresh create_deep_agent orchestrator. Call once per OrchestratorTask."""
    capabilities = render_capabilities_block(list(deps.subagents.values()))
    skills_index = skill_registry.index_block(agent="orchestrator") if skill_registry else ""
    system_prompt = render_system_prompt(capabilities, skills_index=skills_index, prefs_block=prefs_block)

    master_tools: list[BaseTool] = [
        _make_ask_user_tool(deps.channels),
        make_recall_tool(deps.store),
    ]
    if skill_registry:
        master_tools.extend(make_skill_tools(skill_registry))
    if deps.search_config is not None:
        # Orchestrator is always research-capable — attach every ws_* tool
        # whose provider key is configured. Cap enforcement is opt-in.
        master_tools.extend(make_search_tools(deps.search_config, cap_enforcer=deps.cap_enforcer))
    if deps.mcp_manager is not None:
        master_tools.extend(deps.mcp_manager.tools_for("orchestrator"))

    # Build subagent specs from registered BaseSubAgent instances.
    # as_deepagents_subagent_spec() already returns {name, description, system_prompt, tools}.
    # Inject subagent_model, ToolFilterMiddleware (suppresses deepagent built-in fs tools),
    # and a per-subagent token budget.
    subagent_specs = []
    for sa in deps.subagents.values():
        spec = sa.as_deepagents_subagent_spec()
        spec["model"] = deps.subagent_model
        spec["middleware"] = [
            ToolFilterMiddleware(),
            BudgetMiddleware(max_input_tokens=deps.subagent_token_budget, role=sa.name),
        ]
        subagent_specs.append(spec)

    budget = BudgetMiddleware(max_input_tokens=budget_tokens, role="orchestrator")

    # Master backend: LocalShellBackend with virtual_mode so the master can
    # use ls/glob if needed. ToolFilterMiddleware hides read_file/write_file/
    # execute/grep from the LLM — subagents do all real filesystem work via tr_*.
    workspace_root = str(deps.workspace_root.resolve()) if deps.workspace_root else "/"

    def _backend_factory(_runtime):
        return LocalShellBackend(
            root_dir=workspace_root,
            virtual_mode=True,
            timeout=10,
            inherit_env=False,
        )

    return create_deep_agent(
        model=model,
        tools=master_tools,
        backend=_backend_factory,
        system_prompt=system_prompt,
        checkpointer=checkpointer if checkpointer is not None else MemorySaver(),
        middleware=[ToolFilterMiddleware(), budget],
        subagents=subagent_specs,
    )


# ---------------------------------------------------------------------------
# ask_user tool
# ---------------------------------------------------------------------------


def _make_ask_user_tool(channels: ChannelRouter) -> BaseTool:
    @tool
    async def ask_user(question: str, options: list[str] | None = None) -> str:
        """Ask the user a clarifying question and wait for the response.

        ``options`` is an optional list of one-word choices; if empty, the user
        responds with free text. Returns the user's response string.
        """
        ask = AskPrompt(
            ask_id=str(uuid.uuid4()),
            title="Orchestrator question",
            body=question,
            options=list(options) if options else [],
            interrupt_value={"type": "orchestrator_ask", "question": question},
        )
        return await channels.post_ask(ask)

    return ask_user
