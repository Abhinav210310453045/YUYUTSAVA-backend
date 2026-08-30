"""Structural equivalence gate for the ADR-001 refactor.

Three jobs:

1. **Ordering rule** — the middleware stack must satisfy the constraints the
   builders currently express only as repeated comments. These are real
   correctness properties, not style.
2. **Profile agreement** — the middleware actually assembled must match what
   ``core/agent_profiles.py`` declares. Closes the loop: profile declares ->
   builder implements -> fingerprint proves.
3. **Snapshot** — a committed golden file. Any structural change to any of the
   six profile/wiring combinations shows up as a diff, which is what makes the
   pipeline refactor safe to land incrementally.

Regenerate the snapshot ONLY when a change is intended:

    .venv/bin/python test/core/test_agent_fingerprint.py --update-snapshot

Run:  .venv/bin/python test/core/test_agent_fingerprint.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_fingerprint import all_fingerprints, diff_fingerprints  # noqa: E402

from yuyutsava.core.agent_profiles import PROFILES_BY_ROLE, Policy, ToolFamily  # noqa: E402

SNAPSHOT = Path(__file__).resolve().parent / "agent_fingerprint_snapshot.json"


# Ordering rules, each scoped to the hook CHAIN it is about.
#
# Order within a chain is decided by list position. Order *between* chains is
# decided by the agent loop and is not a property of the list — so a rule that
# pairs a tool hook with a before-model hook is not checking what its comment
# claims. Three of the four rules here used to do exactly that; see
# `CrossChainFactsAreNotListOrder` below for what replaced them.
_ORDER_RULES: tuple[tuple[str, str, str, str], ...] = (
    (
        "after_model",
        "BudgetPolicy",
        "UsagePolicy",
        "usage accounting is passive and records the same usage the budget "
        "enforced, so it must see the post-budget state",
    ),
    (
        "before_model",
        "YuyutsavaCompactionMiddleware",
        "TranscriptRecorderPolicy",
        "the transcript records what the model is actually sent, which is the "
        "compacted message list, not the pre-compaction one",
    ),
    (
        "before_model",
        "TranscriptRecorderPolicy",
        "PromptInspectorPolicy",
        "the inspector reports the final assembled context, so it runs last",
    ),
    (
        "model_call",
        "ToolFilterPolicy",
        "RetrievalInjectionPolicy",
        "nothing may observe tools the filter is supposed to have removed",
    ),
)


class MiddlewareOrderingRule(unittest.TestCase):
    """The ordering constraints hold for every profile, in every configuration."""

    def test_order_rules_hold(self) -> None:
        for key, fp in all_fingerprints().items():
            for chain, earlier, later, why in _ORDER_RULES:
                seq = fp["chains"][chain]
                if earlier not in seq or later not in seq:
                    continue  # not both wired in this configuration
                with self.subTest(profile=key, rule=f"{chain}:{earlier}<{later}"):
                    self.assertLess(
                        seq.index(earlier),
                        seq.index(later),
                        f"{key}: in the {chain} chain, {earlier} must run BEFORE "
                        f"{later} — {why}.\nActual: {seq}",
                    )

    def test_tool_filter_leads_the_model_call_chain(self) -> None:
        """Nothing may observe tools the filter is supposed to have removed.

        Scoped to the ``model_call`` chain, not to the whole middleware list.
        Entries in other chains cannot see the tool list at all, so their
        position relative to the filter carries no meaning — and asserting
        against the whole list is what would break when the per-policy adapters
        are collapsed, for no behavioural reason.
        """
        for key, fp in all_fingerprints().items():
            with self.subTest(profile=key):
                chain = fp["chains"]["model_call"]
                self.assertTrue(chain, f"{key}: no model-call policies at all")
                self.assertEqual(
                    chain[0], "ToolFilterPolicy",
                    f"{key}: ToolFilterPolicy must lead the model-call chain; "
                    f"got {chain}",
                )

    def test_every_order_rule_actually_fires(self) -> None:
        """Negative control for the rules themselves.

        ``test_order_rules_hold`` skips a rule whose two names are not both in
        the chain. That is correct for a configuration where one is unwired —
        and catastrophic when a name simply *changed*, which happened twice in
        Phase 4: rules referencing ``ToolResultOffloadMiddleware`` and
        ``ToolFilterMiddleware`` quietly stopped asserting anything while the
        suite stayed green.

        A rule that fires in **no** configuration is not a rule.
        """
        fingerprints = all_fingerprints()
        for chain, earlier, later, why in _ORDER_RULES:
            fired = sum(
                1 for fp in fingerprints.values()
                if earlier in fp["chains"][chain] and later in fp["chains"][chain]
            )
            with self.subTest(rule=f"{chain}:{earlier}<{later}"):
                self.assertGreater(
                    fired, 0,
                    f"the rule '{earlier} before {later}' in the {chain} chain "
                    f"fires in NO configuration, so it enforces nothing. Either "
                    f"a name is stale or the behaviour is gone. ({why})",
                )


class CrossChainFactsAreNotListOrder(unittest.TestCase):
    """Some orderings are real but are not properties of the middleware list.

    "Offload must precede compaction so the compactor counts post-offload
    tokens" is true — and it has nothing to do with list position. Offload is a
    ``wrap_tool_call`` hook; compaction is a ``before_model`` hook. Tool results
    are written to state during tool execution, which is always before the next
    model call, whatever order the two sit in.

    The same goes for "compaction before budget" (a ``before_model`` hook always
    precedes an ``after_model`` hook within a turn) and "tool filter before
    offload" (different chains again).

    These were asserted as list-order rules for three phases. They passed, and
    they would have kept passing however the list was arranged, which means they
    were not enforcing what their comments said. What is actually worth pinning
    is that the two really are in different chains — because if one ever moved
    chains, the loop-structure argument would stop holding and a real rule would
    be needed.
    """

    _CROSS_CHAIN = (
        ("ToolResultOffloadPolicy", "tool_after",
         "YuyutsavaCompactionMiddleware", "before_model"),
        ("YuyutsavaCompactionMiddleware", "before_model",
         "BudgetPolicy", "after_model"),
        ("ToolFilterPolicy", "model_call",
         "ToolResultOffloadPolicy", "tool_after"),
    )

    def test_each_pair_is_still_in_different_chains(self) -> None:
        wired = [fp for key, fp in all_fingerprints().items() if key.endswith(":wired")]
        self.assertTrue(wired)
        for fp in wired:
            for a, chain_a, b, chain_b in self._CROSS_CHAIN:
                with self.subTest(pair=f"{a}/{b}"):
                    self.assertIn(
                        a, fp["chains"][chain_a],
                        f"{a} left the {chain_a} chain; the loop-structure "
                        f"argument no longer covers its order against {b}, so "
                        f"this needs a real ordering rule",
                    )
                    self.assertIn(
                        b, fp["chains"][chain_b],
                        f"{b} left the {chain_b} chain; see above",
                    )
                    self.assertNotEqual(chain_a, chain_b)


class ProfileAgreement(unittest.TestCase):
    """Assembled middleware matches the declared capability matrix."""

    #: Policies whose presence depends on wiring the caller supplies, so they
    #: are only asserted on the ``:wired`` fingerprints.
    _WIRING_DEPENDENT = {Policy.BUDGET, Policy.USAGE}

    def test_declared_policies_are_assembled(self) -> None:
        for key, fp in all_fingerprints().items():
            role, _, mode = key.partition(":")
            profile = PROFILES_BY_ROLE[role]
            # The flattened behaviour set, not the ordered middleware list: since
            # Phase 4 a policy may be attached inside LangChainPolicyAdapter
            # rather than as its own class, and "is it attached" must not depend
            # on which.
            stack = set(fp["policies"])
            for policy in Policy:
                if mode == "bare" and policy in self._WIRING_DEPENDENT:
                    continue
                with self.subTest(profile=key, policy=policy.value):
                    self.assertEqual(
                        policy in profile.policies,
                        policy.value in stack,
                        f"{key}: profile declares {policy.value}="
                        f"{policy in profile.policies} but the assembled stack "
                        f"says {policy.value in stack}.\nStack: {fp['middleware']}",
                    )


class InjectorChain(unittest.TestCase):
    """The assembled injector chain matches the declared profile.

    Took over from the static check in ``test_agent_profiles.py`` once all three
    builders started assembling injectors through one shared helper. Runtime
    inspection is more precise: it sees the chain's *order* and the SkillInjector
    *scope*, neither of which is visible in the builder source any more.
    """

    _MARKER = "RetrievalInjectionPolicy["

    def _chain(self, fp: dict) -> list[str]:
        """Injector names from the RetrievalInjectionPolicy digest, in order.

        The policy now sits inside ``LangChainPolicyAdapter[...]``, so the marker
        is searched for rather than matched at the start. ``_digest`` below
        exists so a stale marker cannot make this silently return ``[]`` and pass.
        """
        return [p.split("(")[0] for p in self._digest(fp).split(",") if p]

    def _digest(self, fp: dict) -> str:
        for entry in fp["middleware"]:
            start = entry.find(self._MARKER)
            if start == -1:
                continue
            inner = entry[start + len(self._MARKER):]
            depth = 1
            for i, ch in enumerate(inner):
                depth += (ch == "[") - (ch == "]")
                if depth == 0:
                    return inner[:i]
        return ""

    def test_the_marker_still_matches_something(self) -> None:
        """Negative control — a stale marker turns every check below vacuous.

        ``_chain`` returns ``[]`` when it finds nothing, and an empty declared
        set compares equal to an empty assembled set, so a renamed policy would
        make this whole class pass while asserting nothing. It nearly did: the
        marker was ``RetrievalInjectionMiddleware[`` until the Phase 4 migration.
        """
        wired = [fp for key, fp in all_fingerprints().items() if key.endswith(":wired")]
        self.assertTrue(wired)
        self.assertTrue(
            any(self._digest(fp) for fp in wired),
            f"no fingerprint contains {self._MARKER!r}; the injector chain is "
            f"not being inspected at all",
        )

    def test_declared_injectors_are_assembled(self) -> None:
        for key, fp in all_fingerprints().items():
            role, _, mode = key.partition(":")
            if mode != "wired":
                continue  # injectors only materialise when their stores are wired
            declared = {i.value for i in PROFILES_BY_ROLE[role].injectors}
            actual = set(self._chain(fp))
            with self.subTest(profile=key):
                self.assertEqual(
                    declared,
                    actual,
                    f"{key}: profile declares injectors {sorted(declared)} but the "
                    f"builder assembled {sorted(actual)}.",
                )

    def test_injector_order_is_stable(self) -> None:
        """Order is a cached-prompt-prefix property, not cosmetics."""
        canonical = [
            "MemoryInjector", "SkillInjector", "ConversationInjector",
            "TodoNoteInjector", "PrefsInjector",
        ]
        for key, fp in all_fingerprints().items():
            if not key.endswith(":wired"):
                continue
            chain = self._chain(fp)
            with self.subTest(profile=key):
                self.assertEqual(
                    chain,
                    [c for c in canonical if c in chain],
                    f"{key}: injector order changed. Injected blocks are "
                    f"concatenated into the prompt in list order, so reordering "
                    f"changes what the model reads first and invalidates the "
                    f"cached prefix.\nExpected subsequence of: {canonical}\nGot: {chain}",
                )

    def test_skill_injector_scope_matches_profile(self) -> None:
        """The CLI is unscoped; the others scope to themselves (12th divergence)."""
        for key, fp in all_fingerprints().items():
            role, _, mode = key.partition(":")
            if mode != "wired":
                continue
            expected = PROFILES_BY_ROLE[role].skill_injector_scope
            # The same marker search as `_chain`, for the same reason: the
            # policy lives inside LangChainPolicyAdapter[...] now, and a
            # `startswith` on the old class name silently yielded "" — which
            # made the unscoped branch below pass on an empty digest.
            entry = self._digest(fp)
            with self.subTest(profile=key):
                if expected is None:
                    self.assertIn(
                        "SkillInjector,", entry + ",",
                        f"{key}: expected an UNSCOPED SkillInjector (searches every "
                        f"agent's skills); digest was {entry}",
                    )
                else:
                    self.assertIn(
                        f"SkillInjector({expected})", entry,
                        f"{key}: expected SkillInjector scoped to {expected!r}; "
                        f"digest was {entry}",
                    )


class ProfileDrivenToolFamilies(unittest.TestCase):
    """Profile-declared tool families produce real tools with matching prefixes.

    ``_shared_master_tools`` selects these families from the profile, so a static
    source check would be circular. This closes the loop non-circularly by
    inspecting the tool names actually bound onto the built agent.

    The `:wired` configuration is used because ctx_*/mem_* only materialise when
    their backing stores are supplied.
    """

    #: Families whose members carry an identifying name prefix.
    _PREFIXES = {
        ToolFamily.CONTEXT: "ctx_",
        ToolFamily.MEMORY: "mem_",
        ToolFamily.VISUALS: "vis_",
        ToolFamily.ARTIFACTS: "artifact_",
        ToolFamily.AGENT_MEMORY: "um_",
        ToolFamily.TODO: "todo_",
        ToolFamily.SKILLS: "sk_",
        ToolFamily.SEARCH: "ws_",
        ToolFamily.TASK_RUNNER: "tr_",
        ToolFamily.DB: "db_",
    }

    def test_declared_families_present_undeclared_absent(self) -> None:
        for key, fp in all_fingerprints().items():
            role, _, mode = key.partition(":")
            if mode != "wired":
                continue
            profile = PROFILES_BY_ROLE[role]
            names = fp["tools"]
            for family, prefix in self._PREFIXES.items():
                present = any(n.startswith(prefix) for n in names)
                with self.subTest(profile=key, family=family.value):
                    self.assertEqual(
                        family in profile.tools,
                        present,
                        f"{key}: profile declares {family.value}="
                        f"{family in profile.tools} but bound tools with prefix "
                        f"{prefix!r} present={present}.\n"
                        f"Bound: {sorted(n for n in names if n.startswith(prefix))}",
                    )

    def test_orchestrator_has_no_execution_tools(self) -> None:
        """The routing boundary, asserted directly.

        The orchestrator handles events and delegates ALL execution to
        subagents. Granting it tr_*/db_*/vis_* would let it bypass that
        boundary — the exact kind of "helpful normalisation" a refactor toward
        uniform builders could introduce by accident.
        """
        names = all_fingerprints()["orchestrator:wired"]["tools"]
        leaked = [n for n in names if n.startswith(("tr_", "db_", "vis_"))]
        self.assertEqual(
            leaked, [],
            f"The orchestrator gained execution tools it must not have: {leaked}. "
            f"It is a plain router — see ORCHESTRATOR_PROFILE and the by-design "
            f"divergence notes in core/agent_profiles.py.",
        )

    def test_todo_scope_produces_the_right_tool_set(self) -> None:
        """`full` scope exposes editing tools; `capture` must not."""
        edit_only = {"todo_update", "todo_set_status", "todo_delete", "todo_add_objective"}
        for key, fp in all_fingerprints().items():
            role, _, mode = key.partition(":")
            if mode != "wired":
                continue
            scope = PROFILES_BY_ROLE[role].todo_scope
            present = {n for n in fp["tools"] if n in edit_only}
            with self.subTest(profile=key, scope=scope):
                if scope == "capture":
                    self.assertEqual(
                        present, set(),
                        f"{key}: todo_scope='capture' but board-editing tools are "
                        f"bound: {sorted(present)}. Only the tinker master may edit "
                        f"the board.",
                    )
                else:
                    self.assertTrue(
                        present,
                        f"{key}: todo_scope={scope!r} but no board-editing tools "
                        f"are bound.",
                    )


class StructuralSnapshot(unittest.TestCase):
    """Golden-file comparison — the actual equivalence gate for ADR-001."""

    def test_matches_snapshot(self) -> None:
        if not SNAPSHOT.exists():
            self.skipTest(
                f"no snapshot yet — create it with:\n"
                f"  .venv/bin/python {Path(__file__).name} --update-snapshot"
            )
        expected = json.loads(SNAPSHOT.read_text())
        actual = all_fingerprints()

        self.assertEqual(
            sorted(expected), sorted(actual),
            "the set of profile/wiring combinations changed",
        )

        problems: list[str] = []
        for key in sorted(expected):
            diffs = diff_fingerprints(expected[key], actual[key])
            if diffs:
                problems.append(f"[{key}]\n  " + "\n  ".join(diffs))

        if problems:
            self.fail(
                "Agent structure changed:\n\n"
                + "\n\n".join(problems)
                + "\n\nIf this change is INTENDED, regenerate the snapshot:\n"
                f"  .venv/bin/python test/core/{Path(__file__).name} --update-snapshot\n"
                "If it is NOT, the refactor altered agent behaviour."
            )


def _update_snapshot() -> None:
    fps = all_fingerprints()
    SNAPSHOT.write_text(json.dumps(fps, indent=2, sort_keys=True) + "\n")
    print(f"wrote {SNAPSHOT.relative_to(Path.cwd())} ({len(fps)} configurations)")
    for key, fp in fps.items():
        print(f"  {key:26} {len(fp['middleware']):2d} middleware, {len(fp['tools']):2d} tools")


if __name__ == "__main__":
    if "--update-snapshot" in sys.argv:
        _update_snapshot()
    else:
        unittest.main(verbosity=2)
