"""deepagents' summarizer is left out of every graph this system builds, for every provider.

``core/harness.exclude_deepagents_summarizer`` registers a harness profile under
the provider deepagents finds for a model (design rule 12). That relies on
deepagents still:

* looking a pre-built model's profile up by its provider;
* honouring ``excluded_middleware`` by the summarizer's public name, in the
  master's stack, the general-purpose subagent's and every declared subagent's;
* leaving a caller's own middleware alone — this system's compactor subclasses
  LangChain's ``SummarizationMiddleware`` and must survive.

Each stack is captured where deepagents hands it to ``create_agent``; nothing is
compiled or sent. One real model per provider type and Vertex family.
"""

from __future__ import annotations

import unittest
import warnings
from typing import Any
from unittest import mock

import pytest

from test.support.targets import every_provider_type, every_type_target
from yuyutsava.context.compaction import YuyutsavaCompactionMiddleware
from yuyutsava.context.config import ContextSettings
from yuyutsava.core.harness import exclude_deepagents_summarizer
from yuyutsava.llm.factory import chat_model

deepagents_graph = pytest.importorskip("deepagents.graph")
harness_profiles = pytest.importorskip("deepagents.profiles.harness.harness_profiles")
subagents_module = pytest.importorskip("deepagents.middleware.subagents")

SUMMARIZER = "SummarizationMiddleware"


def _models() -> dict[str, Any]:
    targets = {
        **every_provider_type(),
        "vertex-claude": every_type_target("vertex/claude-sonnet-4-6"),
        "vertex-maas": every_type_target("vertex/openai/gpt-oss-120b-maas"),
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return {name: chat_model(target) for name, target in targets.items()}


def stacks_built_on(model: Any, *, exclude: bool) -> list[list[str]]:
    """The middleware names of every stack ``create_deep_agent`` builds on *model*."""
    from deepagents import create_deep_agent

    stacks: list[list[str]] = []

    def capture(*_args: Any, middleware=(), **_kwargs: Any) -> Any:
        stacks.append([m.name for m in middleware])
        return mock.MagicMock()

    compactor = YuyutsavaCompactionMiddleware(model=model, settings=ContextSettings())
    declared = {"name": "helper", "description": "helps", "system_prompt": "help"}
    with (
        mock.patch.dict(harness_profiles._HARNESS_PROFILES),
        mock.patch.object(deepagents_graph, "create_agent", capture),
        mock.patch.object(subagents_module, "create_agent", capture),
    ):
        if exclude:
            exclude_deepagents_summarizer(model)
        create_deep_agent(model=model, middleware=[compactor], subagents=[declared])
    return stacks


def summarizer_left_in() -> list[str]:
    """``"<provider>: <stack>"`` for each stack that still carries deepagents' summarizer."""
    found = []
    for name, model in _models().items():
        stacks = stacks_built_on(model, exclude=True)
        if len(stacks) < 3:
            found.append(f"{name}: only {len(stacks)} stacks were built")
        found += [f"{name}: {stack}" for stack in stacks if SUMMARIZER in stack]
    return found


def test_every_provider_builds_without_deepagents_summarizer() -> None:
    left = summarizer_left_in()
    assert not left, (
        "deepagents' summarizer is back in a graph; core/harness.py no longer excludes it:\n  "
        + "\n  ".join(left)
    )


def test_this_systems_compactor_survives_the_exclusion() -> None:
    model = _models()["vertex"]
    master = stacks_built_on(model, exclude=True)[-1]
    assert "YuyutsavaCompactionMiddleware" in master, master


class TheCheckFails(unittest.TestCase):
    """Negative control: without the registration, every stack has the summarizer."""

    def test_without_the_exclusion_the_summarizer_is_there(self) -> None:
        stacks = stacks_built_on(_models()["anthropic"], exclude=False)
        self.assertTrue(stacks)
        self.assertTrue(all(SUMMARIZER in stack for stack in stacks), stacks)


if __name__ == "__main__":
    unittest.main()
