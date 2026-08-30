"""Keep ``core/agent_profiles.py`` honest about ``core/engine.py``.

The capability matrix is only worth having if it matches reality. This asserts
that every capability a profile declares is actually wired by the corresponding
builder, and — just as important — that every capability a profile *omits* is
actually absent.

Method: static analysis of the builder function bodies. Runtime introspection
would be stronger, but ``build_orchestrator`` needs a fully populated
``OrchestratorDeps`` (channels, stores, a live subagent roster), which makes it
far too heavy for a test that should run in milliseconds. The static check
catches the failure mode that actually happens: someone adds ``PrefsInjector``
to one builder and does not update the matrix.

Tool families need indirection handling: the CLI and tinker builders obtain
``ws_*`` / ``sk_*`` / ``db_*`` / ``tr_*`` through
``_build_tool_registry_and_tools``, so a naive scan of their own bodies would
miss them. The helper's source is folded in when a builder calls it.

Run directly:  .venv/bin/python test/core/test_agent_profiles.py
"""

from __future__ import annotations

import inspect
import unittest

from yuyutsava.core import engine
from yuyutsava.core.agent_profiles import (
    ALL_PROFILES,
    CLI_PROFILE,
    ORCHESTRATOR_PROFILE,
    TINKER_PROFILE,
    AgentProfile,
    Injector,
    Policy,
    ToolFamily,
    divergences,
    unreviewed_divergences,
)

# Profile -> the builder that must implement it.
_BUILDERS = {
    CLI_PROFILE.role: engine.build_cli_deepagent,
    ORCHESTRATOR_PROFILE.role: engine.build_orchestrator,
    TINKER_PROFILE.role: engine.build_tinker_agent,
}

# Tool family -> symbols whose presence proves the family is wired. A family may
# have several accepted markers because the builders reach the same tools by
# different routes: the CLI and tinker take a pre-resolved ``mcp_tools`` list,
# while the orchestrator resolves its own via ``deps.mcp_manager.tools_for(...)``.
# Same capability, two wiring styles — which is itself a small instance of the
# duplication ADR-001 removes.
_TOOL_MARKERS: dict[ToolFamily, tuple[str, ...]] = {
    ToolFamily.CONTEXT: ("make_context_tools",),
    ToolFamily.MEMORY: ("make_memory_tools",),
    ToolFamily.VISUALS: ("make_visual_tools",),
    ToolFamily.ARTIFACTS: ("make_artifact_tools",),
    ToolFamily.AGENT_MEMORY: ("make_agent_memory_tools",),
    ToolFamily.TODO: ("make_todo_tools",),
    ToolFamily.SKILLS: ("make_skill_tools",),
    ToolFamily.SEARCH: ("make_search_tools",),
    ToolFamily.TASK_RUNNER: ("_bind_task_runner_tools",),
    ToolFamily.DB: ("make_db_tools",),
    ToolFamily.MCP: ("mcp_tools", "mcp_manager"),
}

# Helpers whose body counts as part of a builder that calls them.
_INLINED_HELPERS = ("_build_tool_registry_and_tools",)


def _effective_source(role: str) -> str:
    """Builder body, plus any helper it delegates tool wiring to."""
    src = inspect.getsource(_BUILDERS[role])
    for helper_name in _INLINED_HELPERS:
        if helper_name in src:
            src += inspect.getsource(getattr(engine, helper_name))
    return src


class AgentProfileConformance(unittest.TestCase):
    """Each declared capability is wired; each undeclared one is absent."""

    def _assert_capability(
        self, profile: AgentProfile, markers: str | tuple[str, ...], declared: bool, label: str
    ) -> None:
        if isinstance(markers, str):
            markers = (markers,)
        src = _effective_source(profile.role)
        present = any(m in src for m in markers)
        if declared and not present:
            self.fail(
                f"{profile.role}: profile declares {label} but "
                f"build_{profile.role}'s source references none of {markers!r}.\n"
                f"Either wire it in core/engine.py, or drop it from "
                f"core/agent_profiles.py — the matrix must not claim capabilities "
                f"the builder does not have."
            )
        if not declared and present:
            self.fail(
                f"{profile.role}: build_{profile.role} references {markers!r} but the "
                f"profile does NOT declare {label}.\n"
                f"A capability was added to the builder without updating the matrix. "
                f"Add it to core/agent_profiles.py so the divergence stays visible."
            )

    # NOTE: policies are NOT checked here any more, for the same reason
    # injectors are not (below). Since Phase 1 step 1.3 all three builders
    # assemble them through the single ``_policy_middleware`` helper, so the
    # class names appear in one shared function rather than per-builder. A
    # source scan can no longer tell the profiles apart — it would report every
    # profile as having every policy.
    #
    # The replacement is strictly stronger:
    # ``test_agent_fingerprint.py`` intercepts the real ``create_deep_agent``
    # kwargs and compares the ASSEMBLED middleware stack against each profile,
    # across 9 configurations. That checks what was actually built rather than
    # what the source mentions — and it is what caught a live behaviour change
    # while 1.3 was being written (the subagent gate had become
    # conditional on ``runtime_settings``, silently dropping it for the
    # standalone CLI).
    #
    # NOTE: injectors are NOT checked here. They used to be, by scanning each
    # builder for ``MemoryInjector`` / ``SkillInjector`` / … — but all three
    # builders now assemble them through the single
    # ``_retrieval_injection_middleware`` helper, so those names appear in one
    # shared function rather than per-builder. A source scan can no longer tell
    # the profiles apart, and folding the helper in would make every profile
    # look like it has every injector.
    #
    # This is the consolidation working as intended, and the replacement is
    # strictly better: ``test_agent_fingerprint.py::InjectorChain`` inspects the
    # injector list actually constructed at build time, in order, including the
    # SkillInjector agent scope — none of which static analysis could see.

    #: Families the builders still wire by hand, through
    #: ``_build_tool_registry_and_tools``. These remain statically checkable
    #: because the builder decides them independently of the profile.
    _REGISTRY_DRIVEN = {
        ToolFamily.SKILLS, ToolFamily.SEARCH, ToolFamily.TASK_RUNNER, ToolFamily.DB,
    }

    def test_registry_driven_tool_families_match_builders(self) -> None:
        """Only the families the builder still chooses for itself.

        The rest (ctx_*, mem_*, vis_*, artifact_*, um_*, todo_*, mcp) are now
        selected by ``_shared_master_tools`` **from the profile**, so a static
        check on them would be circular: the helper wires the family precisely
        because the profile declares it, so the assertion could never fail.

        Those are verified for real in
        ``test_agent_fingerprint.py::ProfileDrivenToolFamilies``, which checks
        the tool names actually bound onto the built agent.
        """
        for profile in ALL_PROFILES:
            for family in self._REGISTRY_DRIVEN:
                with self.subTest(role=profile.role, family=family.value):
                    self._assert_capability(
                        profile, _TOOL_MARKERS[family], family in profile.tools, family.value
                    )

    # todo_scope and agent_memory_namespace are also profile-driven now
    # (``_shared_master_tools`` reads them straight off the profile), so static
    # checks on them are circular. Both are verified against the real bound
    # tools in test_agent_fingerprint.py::ProfileDrivenToolFamilies.

    def test_docker_support_matches_builder(self) -> None:
        for profile in ALL_PROFILES:
            with self.subTest(role=profile.role):
                self.assertEqual(
                    profile.supports_docker,
                    "DockerSandboxBackend" in _effective_source(profile.role),
                    f"{profile.role}: supports_docker={profile.supports_docker} "
                    f"disagrees with the builder.",
                )

    def test_total_divergence_count_is_tracked(self) -> None:
        """Ratchet on the total. Movement here should always be deliberate.

        Baseline history:
          11  initial transcription from the three builders
          12  + skill_injector_scope — surfaced by the fingerprint harness
           8  -4 after the 2026-08-08 decisions: every master scopes skills to
              itself, tinker gained remote-async + the subagent gate, and the
              orchestrator now strips the filesystem prompt block. Those four
              capabilities are now UNIFORM, so they are no longer divergences.
        """
        found = divergences()
        self.assertEqual(
            len(found),
            8,
            "The number of capabilities differing across the three masters changed "
            f"from 12 to {len(found)}.\nDivergent now: {sorted(found)}\n"
            "If deliberate, update this baseline. If not, a master just silently "
            "gained or lost a capability its siblings have.",
        )

    def test_unreviewed_divergence_count_is_tracked(self) -> None:
        """The number that actually matters: divergences nobody has ruled on.

        A divergence with a recorded rationale is an architectural decision and
        must be PRESERVED by ADR-001. One without is an open question, and the
        danger of a normalising refactor is that it answers such questions
        silently, by accident.

        Driving this to zero means *deciding*, not necessarily changing code:
        adding a ``DivergenceNote(by_design=True)`` is a valid resolution.

        Baseline history:
          4  after the orchestrator's routing boundary, unattended operation,
             card-pinning, Docker and build-time prefs were confirmed intentional
          0  after the remaining four were decided on 2026-08-08 — three by
             changing the code to make the capability uniform, one (PrefsInjector)
             by recording that the mechanisms differ but the effect does not.

        **This should stay at 0.** A non-zero value means a master gained or lost
        a capability without anyone deciding it should.
        """
        unreviewed = unreviewed_divergences()
        self.assertEqual(
            len(unreviewed),
            0,
            f"Unreviewed divergences changed from 4 to {len(unreviewed)}: "
            f"{sorted(unreviewed)}\n"
            "If you resolved one, record the decision as a DivergenceNote in "
            "core/agent_profiles.py. If a new one appeared, a master gained or "
            "lost a capability without anyone deciding it should.",
        )



class PoliciesAreProfileDriven(unittest.TestCase):
    """Phase 1 step 1.3: the builders READ ``profile.policies``, not a literal list.

    Before this, each of the three masters wrote its own policy list out in
    full. They agreed — but only by hand, and a fourth master meant a fourth
    copy, which is exactly the cost ADR-001 exists to remove.
    """

    def test_no_builder_hand_writes_the_policy_list(self) -> None:
        import inspect

        from yuyutsava.core import engine

        for fn in ("build_cli_deepagent", "build_orchestrator", "build_tinker_agent"):
            src = inspect.getsource(getattr(engine, fn))
            with self.subTest(builder=fn):
                self.assertIn(
                    "_policy_middleware(", src,
                    f"{fn} does not build its policies from its profile",
                )
                # The literal opener of the old hand-written list. Kept as the
                # pre-Phase-4 class names on purpose: this asserts the OLD shape
                # never comes back, and those names are what it looked like.
                self.assertNotIn(
                    "ToolFilterMiddleware(),\n        FilesystemPromptOverrideMiddleware(),",
                    src,
                    f"{fn} still hand-writes the policy list; adding a policy "
                    f"now means editing three builders again",
                )

    def test_ordering_lives_outside_the_profile(self) -> None:
        """A profile is a *set*. Order is a separate fact, deliberately.

        Putting ordering in the profile would have meant a list, and a list
        invites "just append it" — which is how budget/usage would end up
        before compaction and start reporting pre-compaction token counts.
        """
        from yuyutsava.core.engine import _POLICY_ORDER_POST, _POLICY_ORDER_PRE

        self.assertEqual(
            set(_POLICY_ORDER_PRE) & set(_POLICY_ORDER_POST), set(),
            "a policy appears in both phases; it would be built twice",
        )
        covered = set(_POLICY_ORDER_PRE) | set(_POLICY_ORDER_POST)
        self.assertEqual(
            covered, set(Policy),
            f"policies declared but never placed in the pipeline: "
            f"{sorted(p.value for p in set(Policy) - covered)} — a profile could "
            f"declare one and it would silently never be built",
        )

    def test_budget_and_usage_run_after_compaction(self) -> None:
        """Both must observe POST-compaction token counts."""
        from yuyutsava.core.engine import _POLICY_ORDER_POST

        self.assertIn(Policy.BUDGET, _POLICY_ORDER_POST)
        self.assertIn(Policy.USAGE, _POLICY_ORDER_POST)

    def test_declared_policies_match_what_is_built(self) -> None:
        """Every profile's policy set is reachable through the pipeline order."""
        from yuyutsava.core.engine import _POLICY_ORDER_POST, _POLICY_ORDER_PRE

        placed = set(_POLICY_ORDER_PRE) | set(_POLICY_ORDER_POST)
        for profile in (CLI_PROFILE, ORCHESTRATOR_PROFILE, TINKER_PROFILE):
            with self.subTest(profile=profile.role):
                missing = profile.policies - placed
                self.assertEqual(missing, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
