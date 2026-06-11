"""Verify FilesystemPromptOverrideMiddleware strips the deepagents filesystem block.

Builds the real CLI deepagent (mirroring the chat REPL: subagents=[GeneralPurposeAgent])
with a fake model that records the exact system prompt + bound tools on the first model
call, then asserts:

  * BLOCK C ("## Filesystem Tools" / "## Execute Tool") is GONE from the system prompt.
  * BLOCK A / B / D survive (## TOOL DISCOVERY, write_todos, ## `task`).
  * "## FOLLOWING CONVENTIONS" is still present (now sourced from our own prompt).
  * Bound tools are unchanged: {write_todos, task, tool_search}.

Doubles as an upgrade tripwire: if a future deepagents version defeats the block matcher,
the first assertion fails loudly instead of silently regressing.

Run directly:  .venv/bin/python test/test_filesystem_prompt_override.py
Or via pytest:  pytest test/test_filesystem_prompt_override.py
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path
from typing import Any

os.environ.setdefault("LLM_PROVIDER", "anthropic")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-fake-key-for-test")

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import yuyutsava.core.engine as engine
from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.core.config import AnthropicSettings, LocalSettings, SearchConfig
from yuyutsava.skills.registry import SkillRegistry

REPO = Path(__file__).resolve().parent.parent


def _render_prompt() -> dict[str, Any]:
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

    orig = engine.chat_model
    engine.chat_model = lambda settings, *, temperature=0.1: CapturingModel(
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
        general_purpose = GeneralPurposeAgent(
            task_runner=task_runner, skill_registry=skill_registry, search_config=search_config
        )
        bundle = engine.build_cli_deepagent(
            workspace,
            settings,
            execution_mode="local",
            local_settings=LocalSettings(),
            permission_check=True,
            search_config=search_config,
            subagents=[general_purpose],
        )
        bundle.agent.invoke(
            {"messages": [{"role": "user", "content": "hi"}]},
            config={"configurable": {"thread_id": "fs-prompt-test"}},
        )
    finally:
        engine.chat_model = orig

    msgs = captured.get("messages") or []
    system = msgs[0] if msgs else None
    text = ""
    if system is not None:
        for blk in system.content_blocks:
            if isinstance(blk, dict) and blk.get("type") == "text":
                text += blk.get("text", "")
    return {"system_text": text, "bound_tools": captured.get("bound_tools") or []}


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


def test_bound_tools_unchanged():
    r = _render_prompt()
    assert set(r["bound_tools"]) == {"write_todos", "task", "tool_search"}, (
        f"unexpected bound tools: {r['bound_tools']}"
    )


if __name__ == "__main__":
    test_filesystem_block_removed()
    test_bound_tools_unchanged()
    print("OK — filesystem block stripped, BLOCKs A/B/D intact, bound tools unchanged.")
