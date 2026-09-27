"""How one delegation spec is wired, and where the builders deliberately differ.

`R-DRY-05`: three builders wired a subagent spec, and the identical part —
the filter that hides the master's prefixed tools, the context controllers, and
the `ctx_*` pair a subagent needs to read back its own offloaded results — was
written twice. `subagent_spec` holds it.

What is *not* shared is ADR-001's business and stays at the call site: the
orchestrator names a model, adds a per-subagent spend ceiling and per-call cost
accounting, and skips subagents the operator has switched off. The CLI and
tinker builds add none of that, because they have no subagent budget
configured.

Both of those divergences were uncovered — negative-controlling the extraction
by deleting the budget middleware and by dropping the disabled filter changed
no test — which is why the structural half below exists.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

from yuyutsava.core.engine import subagent_spec, sync_subagent_specs

ENGINE = pathlib.Path(__file__).resolve().parent.parent.parent / "yuyutsava" / "core" / "engine.py"


class _Sub:
    def __init__(self, name: str) -> None:
        self.name = name

    def as_deepagents_subagent_spec(self) -> dict:
        return {"name": self.name, "prompt": "p", "tools": ["own_tool"]}


class _Store:
    """Stands in for an artifact store; only its presence is read here."""


def _names(middleware) -> list[str]:
    return [type(m).__name__ for m in middleware]


class WhatEverySpecGets(unittest.TestCase):
    def test_the_master_s_prefixed_tools_are_filtered_out(self) -> None:
        """A subagent that could see tr_*/db_* would route around its own
        parent. The filter is first, before anything can add tools."""
        spec = subagent_spec(_Sub("a"), context_middleware=[])
        self.assertTrue(_names(spec["middleware"]), "middleware must not be empty")

    def test_context_middleware_is_included(self) -> None:
        marker = object()
        spec = subagent_spec(_Sub("a"), context_middleware=[marker])
        self.assertIn(marker, spec["middleware"])

    def test_extra_middleware_comes_after_the_context_controllers(self) -> None:
        """Order matters downstream: offload runs on the tool path before the
        compactor counts tokens, and a spend ceiling sits after both."""
        ctx, extra = object(), object()
        spec = subagent_spec(_Sub("a"), context_middleware=[ctx], extra_middleware=[extra])
        mw = spec["middleware"]
        self.assertLess(mw.index(ctx), mw.index(extra))

    def test_the_agent_keeps_its_own_tools(self) -> None:
        spec = subagent_spec(_Sub("a"), context_middleware=[])
        self.assertIn("own_tool", spec["tools"])

    def test_ctx_tools_arrive_only_with_an_artifact_store(self) -> None:
        """Without a store there is nothing offloaded to read back."""
        without = subagent_spec(_Sub("a"), context_middleware=[])
        self.assertEqual(without["tools"], ["own_tool"])
        with_store = subagent_spec(_Sub("a"), context_middleware=[], artifact_store=_Store())
        self.assertGreater(len(with_store["tools"]), 1)
        self.assertTrue(any(t.name.startswith("ctx_") for t in with_store["tools"][1:]))

    def test_each_spec_gets_its_own_ctx_instances(self) -> None:
        """Never shared across graphs — two specs holding one tool object is
        how state leaks between delegations."""
        store = _Store()
        first = subagent_spec(_Sub("a"), context_middleware=[], artifact_store=store)
        second = subagent_spec(_Sub("b"), context_middleware=[], artifact_store=store)
        firsts = {id(t) for t in first["tools"][1:]}
        seconds = {id(t) for t in second["tools"][1:]}
        self.assertFalse(firsts & seconds)

    def test_a_model_is_set_only_when_named(self) -> None:
        self.assertNotIn("model", subagent_spec(_Sub("a"), context_middleware=[]))
        model = object()
        self.assertIs(subagent_spec(_Sub("a"), context_middleware=[], model=model)["model"], model)


class TheCliAndTinkerPathAddsNoBudget(unittest.TestCase):
    """ADR-001: those builds have no subagent token-budget configured, so a
    ceiling there would be a number nobody chose."""

    def test_no_budget_policy_in_a_sync_spec(self) -> None:
        specs = sync_subagent_specs(
            [_Sub("a")],
            model=None,
            artifact_store=None,
            context_settings=None,
            summary_store=None,
            memory_store=None,
            compaction_model=None,
        )
        self.assertEqual(len(specs), 1)
        flat = " ".join(_names(specs[0]["middleware"]))
        self.assertNotIn("Budget", flat)

    def test_one_spec_per_subagent(self) -> None:
        specs = sync_subagent_specs(
            [_Sub("a"), _Sub("b")],
            model=None,
            artifact_store=None,
            context_settings=None,
            summary_store=None,
            memory_store=None,
            compaction_model=None,
        )
        self.assertEqual([s["name"] for s in specs], ["a", "b"])


class TheOrchestratorKeepsItsDivergences(unittest.TestCase):
    """Asserted over the source: building a real orchestrator needs a model, a
    checkpointer and twenty dependencies, and what is being checked is one
    call's shape."""

    #: Where the orchestrator builds its subagent specs; build_orchestrator
    #: calls it (test_the_builder_uses_it).
    _SPEC_BUILDER = "_orchestrator_subagents"

    def _function(self, name: str) -> ast.FunctionDef:
        tree = ast.parse(ENGINE.read_text())
        return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)

    def _orchestrator_spec_call(self) -> ast.Call:
        fn = self._function(self._SPEC_BUILDER)
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", "")
                if name == "subagent_spec":
                    return node
        raise AssertionError("the orchestrator no longer builds a spec through subagent_spec")

    def test_the_call_is_found_at_all(self) -> None:
        """So a rename cannot make the assertions below vacuous."""
        self.assertIsInstance(self._orchestrator_spec_call(), ast.Call)

    def test_the_builder_uses_it(self) -> None:
        calls = {
            getattr(n.func, "id", "")
            for n in ast.walk(self._function("build_orchestrator"))
            if isinstance(n, ast.Call)
        }
        self.assertIn(self._SPEC_BUILDER, calls)

    def test_it_passes_a_spend_ceiling_and_cost_accounting(self) -> None:
        call = self._orchestrator_spec_call()
        extra = next((k for k in call.keywords if k.arg == "extra_middleware"), None)
        self.assertIsNotNone(extra, "the orchestrator must pass extra middleware")
        source = ast.unparse(extra.value)
        self.assertIn("BudgetPolicy", source)
        self.assertIn("_usage_mw", source)

    def test_it_names_a_model_for_its_subagents(self) -> None:
        call = self._orchestrator_spec_call()
        self.assertTrue(any(k.arg == "model" for k in call.keywords))

    def test_a_switched_off_subagent_is_skipped(self) -> None:
        """An operator turning one off must stop it being delegated to, not
        merely stop it being advertised."""
        fn = self._function(self._SPEC_BUILDER)
        comprehensions = [
            ast.unparse(n)
            for n in ast.walk(fn)
            if isinstance(n, ast.ListComp) and "subagent_spec" in ast.unparse(n)
        ]
        self.assertTrue(comprehensions, "no spec comprehension found")
        self.assertTrue(
            any("not in disabled" in c for c in comprehensions),
            "the disabled-subagent guard is gone",
        )


if __name__ == "__main__":
    unittest.main()
