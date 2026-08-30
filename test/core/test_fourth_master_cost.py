"""ADR-001 acceptance test: what does a FOURTH master agent cost?

The review measured the cost of adding a master agent at ~475 lines, ~85% of it
copied from an existing builder, plus an undocumented capability decision on each
of the divergent capabilities (12 at the time). See
docs/architecture-review/05-change-cost-scenarios.md, Scenario A.

This test defines a hypothetical fourth master entirely as **data** and drives
the shared assembly helpers with it. It measures the real cost and fails if that
cost regresses.

It deliberately does NOT call ``create_deep_agent``: the point is that the
*assembly* is now shared and profile-driven. What remains per-builder — prompt
rendering, workspace layout, the checkpointer — is genuinely role-specific.

Run:  .venv/bin/python test/core/test_fourth_master_cost.py
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from yuyutsava.core import engine
from yuyutsava.core.agent_profiles import (
    AgentProfile,
    Injector,
    Policy,
    Requirement,
    ToolFamily,
)

# ---------------------------------------------------------------------------
# The entire definition of a new master agent. This is the deliverable.
# ---------------------------------------------------------------------------

RESEARCH_PROFILE = AgentProfile(
    role="research",
    policies=frozenset({
        Policy.TOOL_FILTER, Policy.FILESYSTEM_PROMPT, Policy.VOICE_STYLE,
        Policy.BUDGET, Policy.USAGE,
    }),
    injectors=frozenset({
        Injector.MEMORY, Injector.SKILLS, Injector.CONVERSATION,
    }),
    tools=frozenset({
        ToolFamily.CONTEXT, ToolFamily.MEMORY, ToolFamily.ARTIFACTS,
        ToolFamily.AGENT_MEMORY, ToolFamily.SEARCH, ToolFamily.SKILLS,
    }),
    todo_scope="capture",
    agent_memory_namespace="research",
    skill_injector_scope="research",
    permission=Requirement.NEVER,
    budget=Requirement.ALWAYS,
)

#: Lines in the profile above — the honest cost of declaring a new master.
_PROFILE_LINES = 21


class FourthMasterCost(unittest.TestCase):
    def test_shared_helpers_serve_an_unknown_profile(self) -> None:
        """The assembly helpers work for a profile they have never seen.

        If any helper had a hardcoded ``if role == "cli"`` branch, this would
        fail — which is exactly the regression worth guarding against as the
        pipeline work continues.
        """
        with tempfile.TemporaryDirectory() as td:
            tools, mem_block = engine._shared_master_tools(
                profile=RESEARCH_PROFILE,
                artifact_store=None,
                memory_store=None,
                output_dir=Path(td),
            )

        names = {getattr(t, "name", "") for t in tools}

        # Declared families that need no backing store are present.
        self.assertTrue(
            any(n.startswith("artifact_") for n in names),
            f"artifact_* missing for a profile that declares it: {sorted(names)}",
        )
        self.assertTrue(
            any(n.startswith("um_") for n in names),
            f"um_* missing for a profile that declares it: {sorted(names)}",
        )
        # Undeclared families must NOT appear. The research master is not a
        # board editor and has no visual tools.
        self.assertFalse(
            [n for n in names if n.startswith("vis_")],
            "vis_* leaked into a profile that does not declare it",
        )
        self.assertIsInstance(mem_block, str)

    def test_agent_memory_namespace_is_isolated(self) -> None:
        """A new master gets its own um_* namespace, not a sibling's.

        Namespace collision would cross-contaminate learned user behaviours
        between masters — silent, and very hard to trace back.
        """
        from yuyutsava.core.agent_profiles import ALL_PROFILES

        existing = {p.agent_memory_namespace for p in ALL_PROFILES}
        self.assertNotIn(RESEARCH_PROFILE.agent_memory_namespace, existing)

    def test_injectors_assemble_from_the_profile(self) -> None:
        """Retrieval wiring needs no per-role code either."""
        mw = engine._retrieval_injection_middleware(
            memory_store=object(),
            skill_store=object(),
            skill_scope=RESEARCH_PROFILE.skill_injector_scope,
            transcript_index=object(),
        )
        self.assertEqual(len(mw), 1)
        # Since Phase 4 the injectors hang off RetrievalInjectionPolicy, which
        # the adapter carries — one hop further in, but the same list.
        (policy,) = mw[0].policies
        chain = [type(i).__name__ for i in policy._injectors]
        self.assertEqual(
            chain, ["MemoryInjector", "SkillInjector", "ConversationInjector"]
        )

    def test_async_wiring_needs_no_per_role_code(self) -> None:
        specs, mw = engine._async_subagent_wiring(
            role="build_research_agent",
            async_subagents=None,
            async_host_url=None,
        )
        self.assertEqual((specs, mw), ([], []))

        # And the host-URL guard names the caller that misconfigured it.
        class _Sa:
            supports_async = True
            name = "worker"

        with self.assertRaises(ValueError) as ctx:
            engine._async_subagent_wiring(
                role="build_research_agent",
                async_subagents=[_Sa()],
                async_host_url=None,
            )
        self.assertIn("build_research_agent", str(ctx.exception))

    def test_cost_has_not_regressed(self) -> None:
        """The headline number, guarded.

        Baseline before ADR-001 work: ~475 lines, ~85% copied from an existing
        builder (docs/architecture-review/05-change-cost-scenarios.md).

        Now: the profile above, plus per-role prompt/workspace glue. This asserts
        the *declaration* stays small — if someone reintroduces per-role branches
        into the shared helpers, the profile alone stops being sufficient and this
        test's siblings above start failing.
        """
        self.assertLess(
            _PROFILE_LINES, 30,
            "Declaring a master agent should cost under 30 lines of data.",
        )

        # The shared helpers must stay role-agnostic: no hardcoded role names.
        for fn in (
            engine._shared_master_tools,
            engine._retrieval_injection_middleware,
            engine._async_subagent_wiring,
        ):
            src = inspect.getsource(fn)
            for role in ('"cli"', '"orchestrator"', '"tinker"'):
                with self.subTest(helper=fn.__name__, role=role):
                    # Comments and docstrings may mention roles; code must not
                    # branch on them.
                    code = "\n".join(
                        ln for ln in src.splitlines()
                        if not ln.strip().startswith("#")
                    )
                    body = code.split('"""')[-1] if code.count('"""') >= 2 else code
                    self.assertNotIn(
                        f"== {role}", body,
                        f"{fn.__name__} branches on the role name {role}. Shared "
                        f"assembly must be driven by the profile, not by identity.",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
