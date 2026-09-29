"""Verify FilesystemPromptPolicy strips the deepagents filesystem block.

Builds the real CLI deepagent (mirroring the chat REPL: subagents=[GeneralPurposeAgent])
with a fake model that records the exact system prompt + bound tools on the first model
call, then asserts:

  * BLOCK C ("## Filesystem Tools" / "## Execute Tool") is GONE from the system prompt.
  * BLOCK A / B / D survive (## TOOL DISCOVERY, write_todos, ## `task`).
  * "## FOLLOWING CONVENTIONS" is still present (now sourced from our own prompt).
  * Bound tools are unchanged: {write_todos, task, tool_search}.

Doubles as an upgrade tripwire: if a future deepagents version defeats the block matcher,
the first assertion fails loudly instead of silently regressing.

Run:  .venv/bin/python -m pytest test/policy/test_filesystem_prompt_override.py
"""

from __future__ import annotations

import asyncio
import itertools
import os
from typing import Any
from unittest import mock

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from mcp.types import Tool

import yuyutsava.core.engine as engine
import yuyutsava.llm.factory as llm_factory
from test.support.paths import REPO
from test.support.stores import fake_board, scratch_visual_store
from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.config.execution import LocalSettings
from yuyutsava.config.search import SearchConfig
from yuyutsava.core.deps import MasterDeps
from yuyutsava.llm.settings import AnthropicSettings
from yuyutsava.mcp.naming import server_of
from yuyutsava.mcp.tool_adapter import adapt
from yuyutsava.skills.registry import SkillRegistry


# Only while building: set at import, it leaked into every later test.
@mock.patch.dict(
    os.environ, {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-fake-key-for-test"}
)
def _render_prompt(mcp_tools: list | None = None) -> dict[str, Any]:
    """Build the CLI deepagent with a capturing fake model and return what it sent."""
    captured: dict[str, Any] = {}

    class CapturingModel(GenericFakeChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
            captured.setdefault("messages", messages)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
            return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

        def bind_tools(self, tools, **kwargs):
            names = []
            for t in tools:
                n = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
                names.append(n or getattr(t, "__name__", str(t)))
            captured.setdefault("bound_tools", names)
            return self

    # Patch the factory where the builders look it up. ``core/engine.py`` imports
    # ``chat_model`` at module level, so ``engine.chat_model`` is the name
    # ``build_cli_deepagent`` calls; ``yuyutsava.llm.factory.chat_model`` is patched
    # too, for callers that look it up on the factory module. Patching only the
    # source module once let a real provider model through: with this fake key it
    # failed with a 401, and with a real key it would have been a billable call.
    orig_llm, orig_engine = llm_factory.chat_model, engine.chat_model
    llm_factory.chat_model = engine.chat_model = lambda settings, **kwargs: CapturingModel(
        messages=itertools.cycle([AIMessage(content="done")])
    )
    try:
        workspace = (REPO / "workspace").resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        settings = AnthropicSettings(api_key="sk-fake", model="claude-haiku-4-5-20251001")
        search_config = SearchConfig(tavily_api_key="dummy", exa_api_key="dummy")
        skill_registry = SkillRegistry(workspace_dir=workspace)
        task_runner = TaskRunnerAgent(
            workspace_root=workspace, sandbox_root=(workspace / "_sandbox").resolve()
        )
        visuals = scratch_visual_store()
        general_purpose = GeneralPurposeAgent(
            task_runner=task_runner,
            skill_registry=skill_registry,
            search_config=search_config,
            visual_store=visuals,
        )
        bundle = engine.build_cli_deepagent(
            workspace,
            settings,
            deps=MasterDeps(
                board=fake_board(),
                visual_store=visuals,
                permission_check=True,
                search_config=search_config,
                subagents=[general_purpose],
                mcp_tools=mcp_tools,
            ),
            execution_mode="local",
            local_settings=LocalSettings(),
        )
        # ``ainvoke``, not ``invoke``: LangChain picks the sync or async
        # middleware hooks from how the graph is driven, and every production
        # path (daemon, CLI, voice) is async. Rendering the prompt through the
        # sync path meant this tripwire was exercising hooks nothing in
        # production runs — it went unnoticed until Phase 4, when the migrated
        # policies became async-only and this was the single caller that broke.
        asyncio.run(
            bundle.agent.ainvoke(
                {"messages": [{"role": "user", "content": "hi"}]},
                config={"configurable": {"thread_id": "fs-prompt-test"}},
            )
        )
    finally:
        llm_factory.chat_model, engine.chat_model = orig_llm, orig_engine

    msgs = captured.get("messages") or []
    system = msgs[0] if msgs else None
    text = ""
    if system is not None:
        for blk in system.content_blocks:
            if isinstance(blk, dict) and blk.get("type") == "text":
                text += blk.get("text", "")
    return {"system_text": text, "bound_tools": captured.get("bound_tools") or []}


def _mcp_tools() -> list:
    """Two tools from a fake MCP server, adapted the way the loader adapts them.

    On a loop, as the loader does: ``adapt`` records the session's home loop.
    Nothing here calls them.
    """

    async def _adapted() -> list:
        return [
            adapt(object(), "probe", Tool(name=f"op_{i:02d}", inputSchema={"type": "object"}))
            for i in range(2)
        ]

    return asyncio.run(_adapted())


def test_filesystem_block_removed():
    r = _render_prompt()
    text = r["system_text"]

    # BLOCK C is gone (Option A: dropped entirely).
    assert "## Filesystem Tools" not in text, "deepagents filesystem block was not stripped"
    assert "## Execute Tool" not in text, "deepagents execute block was not stripped"

    # BLOCKs A / B / D survive.
    assert "## TOOL DISCOVERY" in text, "BLOCK A (our prompt) missing"
    assert "write_todos" in text, "BLOCK B (todos) missing"
    assert "## `task`" in text, "BLOCK D (subagents) missing"

    # Following Conventions preserved via our own prompt.
    assert "## FOLLOWING CONVENTIONS" in text, "Following Conventions guidance was lost"


def test_lazy_discovery_invariant_holds():
    """Suppressed tool families never reach the model; discovery entry point does.

    Asserts the *invariant* rather than an exact tool list. The previous version
    pinned ``{write_todos, task, tool_search}`` exactly, so it broke the moment
    the always-visible vis_* / artifact_* families were added — a false alarm
    that says nothing about whether lazy discovery still works. What actually
    matters is that ToolFilterPolicy is still hiding what it claims to hide.
    """
    from yuyutsava.policy.builtin.tool_filter import SUPPRESS_NAMES, SUPPRESS_PREFIXES

    rendered = _render_prompt(mcp_tools=_mcp_tools())
    bound = set(rendered["bound_tools"])

    leaked_prefix = {n for n in bound if n.startswith(SUPPRESS_PREFIXES)}
    assert not leaked_prefix, (
        f"suppressed prefix families leaked to the model: {sorted(leaked_prefix)}"
    )

    # MCP server tools too (R-BUG-26): named in the catalog, schema on demand.
    leaked_mcp = {n for n in bound if server_of(n) is not None}
    assert not leaked_mcp, f"MCP tool schemas leaked to the model: {sorted(leaked_mcp)}"
    assert "probe__*:" in rendered["system_text"], "MCP tools missing from the catalog"
    assert "probe__op_00" in rendered["system_text"], "an MCP tool is not named"

    leaked_builtin = bound & SUPPRESS_NAMES
    assert not leaked_builtin, (
        f"deepagents built-ins we replace with tr_* leaked to the model: {sorted(leaked_builtin)}"
    )

    # The lazy-discovery entry point and the deepagents built-ins we keep.
    assert "tool_search" in bound, "tool_search missing — lazy discovery is broken"
    assert {"write_todos", "task"} <= bound, (
        f"expected deepagents built-ins missing: {sorted(bound)}"
    )


if __name__ == "__main__":
    test_filesystem_block_removed()
    test_lazy_discovery_invariant_holds()
    print("OK — filesystem block stripped, BLOCKs A/B/D intact, lazy-discovery invariant holds.")
