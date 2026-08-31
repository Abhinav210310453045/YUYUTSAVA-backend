"""The general-purpose subagent must always carry ws_* web-search tools when a
search provider is configured — independent of any skill's ``requires_tools``.

Regression test for the bug where the orchestrator delegated an internet-search
task to ``subagent_type=general-purpose`` and the subagent refused, because its
tool catalog had no ws_* entries: the base ``search_tools()`` only attaches a
ws_* tool a *visible skill* declares, and no general-purpose-scoped skill does.

Asserts against ``all_tools()`` so it exercises the real assembly path, not the
override in isolation.

Run directly:  .venv/bin/python -m pytest test/test_general_purpose_search_tools.py -v
"""

from __future__ import annotations

from pathlib import Path

from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.core.config import SearchConfig
from yuyutsava.skills.registry import SkillRegistry

REPO = Path(__file__).resolve().parent.parent

WS_ALL = {"ws_tavily_search", "ws_exa_search", "ws_exa_get_contents"}


def _agent(search_config: SearchConfig) -> GeneralPurposeAgent:
    workspace = (REPO / "workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    task_runner = TaskRunnerAgent(
        workspace_root=workspace, sandbox_root=(workspace / "_sandbox").resolve()
    )
    skill_registry = SkillRegistry(workspace_dir=workspace)
    return GeneralPurposeAgent(
        task_runner=task_runner,
        skill_registry=skill_registry,
        search_config=search_config,
    )


def _tool_names(agent: GeneralPurposeAgent) -> set[str]:
    return {t.name for t in agent.all_tools()}


def test_both_providers_present() -> None:
    agent = _agent(SearchConfig(tavily_api_key="dummy", exa_api_key="dummy"))
    names = _tool_names(agent)
    assert WS_ALL <= names, f"expected all ws_* tools, got {sorted(n for n in names if n.startswith('ws_'))}"


def test_no_keys_means_no_web_search() -> None:
    agent = _agent(SearchConfig())
    names = _tool_names(agent)
    assert not any(n.startswith("ws_") for n in names), names


def test_single_provider_gating_honored() -> None:
    agent = _agent(SearchConfig(tavily_api_key="dummy"))
    names = _tool_names(agent)
    assert "ws_tavily_search" in names
    assert "ws_exa_search" not in names
    assert "ws_exa_get_contents" not in names


def test_no_duplicate_tool_names() -> None:
    agent = _agent(SearchConfig(tavily_api_key="dummy", exa_api_key="dummy"))
    all_names = [t.name for t in agent.all_tools()]
    assert len(all_names) == len(set(all_names)), all_names


if __name__ == "__main__":
    import sys
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
