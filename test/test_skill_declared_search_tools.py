"""Which ws_* tools a subagent carries, and why there are two rules.

The default is skill-driven: a subagent sees only the ws_* tools at least one
of its visible skills names in ``requires_tools``. That replaced a blanket
attach, and it is a real restriction — `file-organizer` reads
``pdf-to-archive``, which names none, so it gets none.

Two agents opt out, because their job is researching things a skill list cannot
anticipate: the generalist fallback and the tinker agent. They used to say so by
each writing the same six-line override (`R-DRY-05`); they now set
``wants_all_search_tools``.

`test_general_purpose_search_tools.py` covers the opted-out side. This covers
the default, which nothing did: negative-controlling the merge by flipping the
base's default to ``True`` changed no test, and that flip silently hands every
subagent every web-search tool.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from yuyutsava.agents.base_sub_agent import BaseSubAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.config.search import SearchConfig

REPO = Path(__file__).resolve().parent.parent
WS_ALL = {"ws_tavily_search", "ws_exa_search", "ws_exa_get_contents"}


class _Skill:
    def __init__(self, requires_tools: list[str]) -> None:
        self.requires_tools = requires_tools


class _Registry:
    """A skill registry that reports exactly what a test wants declared."""

    def __init__(self, *skills: _Skill) -> None:
        self._skills = skills

    def scan(self, agent: str | None = None) -> tuple[_Skill, ...]:
        return self._skills


class _Subagent(BaseSubAgent):
    name = "test-subagent"
    description = "for tests"

    def system_prompt(self) -> str:
        return "you are a test"


def _agent(registry, *, keys: bool = True, all_tools: bool = False) -> _Subagent:
    workspace = (REPO / "workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    runner = TaskRunnerAgent(
        workspace_root=workspace, sandbox_root=(workspace / "_sandbox").resolve()
    )
    config = SearchConfig(tavily_api_key="dummy", exa_api_key="dummy") if keys else SearchConfig()
    agent = _Subagent(task_runner=runner, skill_registry=registry, search_config=config)
    if all_tools:
        agent.wants_all_search_tools = True
    return agent


def _ws(agent: _Subagent) -> set[str]:
    return {t.name for t in agent.search_tools()}


class TheDefaultIsSkillDriven(unittest.TestCase):
    def test_a_skill_naming_nothing_gets_nothing(self) -> None:
        """file-organizer's case, and the point of the restriction."""
        self.assertEqual(_ws(_agent(_Registry(_Skill([])))), set())

    def test_no_skills_at_all_gets_nothing(self) -> None:
        self.assertEqual(_ws(_agent(_Registry())), set())

    def test_a_skill_gets_only_what_it_names(self) -> None:
        got = _ws(_agent(_Registry(_Skill(["ws_tavily_search"]))))
        self.assertEqual(got, {"ws_tavily_search"})

    def test_a_pattern_is_matched(self) -> None:
        """`requires_tools` entries are fnmatch patterns, not exact names."""
        self.assertEqual(
            _ws(_agent(_Registry(_Skill(["ws_exa_*"])))),
            {
                "ws_exa_search",
                "ws_exa_get_contents",
            },
        )

    def test_declarations_across_skills_add_up(self) -> None:
        got = _ws(_agent(_Registry(_Skill(["ws_tavily_search"]), _Skill(["ws_exa_search"]))))
        self.assertEqual(got, {"ws_tavily_search", "ws_exa_search"})

    def test_no_registry_means_nothing_declared(self) -> None:
        self.assertEqual(_ws(_agent(None)), set())


class OptingOutTakesEverything(unittest.TestCase):
    def test_the_full_set_arrives_without_any_skill_naming_it(self) -> None:
        self.assertEqual(_ws(_agent(_Registry(_Skill([])), all_tools=True)), WS_ALL)

    def test_it_still_needs_a_configured_provider(self) -> None:
        """Opting out asks for every *configured* tool, not for tools that
        cannot work."""
        self.assertEqual(_ws(_agent(_Registry(_Skill([])), keys=False, all_tools=True)), set())

    def test_a_declared_tool_is_not_duplicated(self) -> None:
        agent = _agent(_Registry(_Skill(["ws_tavily_search"])), all_tools=True)
        names = [t.name for t in agent.search_tools()]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), WS_ALL)

    def test_no_registry_is_fine_when_opted_out(self) -> None:
        """The old override checked only the search config, and an agent with
        no registry still got the full set. That has to hold."""
        self.assertEqual(_ws(_agent(None, all_tools=True)), WS_ALL)


class WithoutAProviderNobodyGetsAnything(unittest.TestCase):
    def test_no_keys_means_no_tools(self) -> None:
        self.assertEqual(_ws(_agent(_Registry(_Skill(["ws_tavily_search"])), keys=False)), set())


if __name__ == "__main__":
    unittest.main()
