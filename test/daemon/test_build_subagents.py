"""``build_subagents`` — the roster, and the sync/background split.

Phase 3 step 3.3, seventh slice. Extracting this made the roster constructible
in a test for the first time: it needs no servers, no database and no models,
so the invariants below could not previously be checked at all.

Two of them are deliberate design decisions rather than incidental structure:

* **The TinkerAgent is background-only.** It is an async peer of the master and
  is *not* in the sync delegation table, because interactive tinkering has its
  own per-card bundle (``ConversationManager``). If it leaked into ``sync``, the
  orchestrator would start running long tinkering jobs inline and block the
  conversation it was called from.
* **The sync agents share one dependency set.** They took the same seven keyword
  arguments written out three times; a typo in one was a silent capability
  difference between siblings. They share a ``**common`` dict now, and this
  suite checks the sharing actually happened.

Run:  .venv/bin/python test/daemon/test_build_subagents.py
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


def _build(**over):
    from yuyutsava.core.config import SearchConfig
    from yuyutsava.daemon.bootstrap import build_subagents

    kw = dict(
        workspace=Path(tempfile.gettempdir()),
        policy=None, consent=None, store=None,
        skill_registry=None, search_config=SearchConfig(),
        mcp_manager=None, cap_enforcer=None,
        memory_store=None, skill_store=None,
    )
    kw.update(over)
    return build_subagents(**kw)


class RosterShape(unittest.TestCase):
    def setUp(self) -> None:
        self.subs = _build()

    def test_three_sync_subagents(self) -> None:
        self.assertEqual(len(self.subs.sync_list), 3)
        self.assertEqual(set(self.subs.sync), {sa.name for sa in self.subs.sync_list})

    def test_sync_dict_and_list_agree(self) -> None:
        """The dict is a view of the list — a divergence means one is stale."""
        self.assertEqual(
            [sa.name for sa in self.subs.sync_list], list(self.subs.sync))

    def test_background_is_sync_plus_tinker(self) -> None:
        self.assertEqual(len(self.subs.background), len(self.subs.sync_list) + 1)
        for sa in self.subs.sync_list:
            self.assertIn(sa, self.subs.background)

    def test_tinker_is_not_a_sync_subagent(self) -> None:
        """The load-bearing one: inline tinkering would block the conversation."""
        names = set(self.subs.sync)
        self.assertNotIn(
            "tinker", names,
            "the TinkerAgent reached the SYNC roster. The orchestrator would "
            "then run long tinkering jobs inline instead of delegating them to "
            "the background host, blocking the chat that asked.",
        )
        self.assertTrue(
            any("tinker" in getattr(sa, "name", "") for sa in self.subs.background),
            "the TinkerAgent is missing from the BACKGROUND roster, so "
            "'tinker on card X in the background' has nothing to delegate to",
        )

    def test_general_purpose_keeps_its_reserved_name(self) -> None:
        """``general-purpose`` suppresses deepagents' built-in default agent."""
        self.assertIn("general-purpose", self.subs.sync)

    def test_task_runner_is_returned(self) -> None:
        """Callers need the same instance the subagents were built around."""
        self.assertIsNotNone(self.subs.task_runner)

    def test_all_sync_agents_share_the_task_runner(self) -> None:
        for sa in self.subs.sync_list:
            with self.subTest(agent=sa.name):
                runner = getattr(sa, "_task_runner", None) or getattr(sa, "task_runner", None)
                if runner is not None:
                    self.assertIs(runner, self.subs.task_runner)


class SharedDependencies(unittest.TestCase):
    """The three sync agents must receive the *same* dependency objects.

    They used to be constructed with seven keyword arguments written out three
    times. Nothing caught a typo that gave one sibling a different store.
    """

    def test_search_config_is_shared(self) -> None:
        from yuyutsava.core.config import SearchConfig

        cfg = SearchConfig()
        subs = _build(search_config=cfg)
        for sa in subs.sync_list:
            with self.subTest(agent=sa.name):
                got = getattr(sa, "_search_config", None) or getattr(sa, "search_config", None)
                if got is not None:
                    self.assertIs(
                        got, cfg,
                        f"{sa.name} got a different SearchConfig than its "
                        f"siblings — the shared **common dict was bypassed",
                    )

    def test_every_sync_agent_can_write_skills(self) -> None:
        """A shared flag; one agent silently lacking it is the failure mode."""
        subs = _build()
        for sa in subs.sync_list:
            with self.subTest(agent=sa.name):
                flag = getattr(sa, "_can_write_skills", None)
                if flag is None:
                    flag = getattr(sa, "can_write_skills", None)
                if flag is not None:
                    self.assertTrue(flag)


class FreshPeersForOtherGraphs(unittest.TestCase):
    """``make_peers`` mints new instances, not the same objects.

    A deepagent spec must not share tool/middleware objects across graphs, so
    the chat master needs its own instances of the store-backed peers. That is
    about identity, not dependencies — hence a factory rather than a second
    hand-written argument list.
    """

    def test_peers_are_distinct_objects(self) -> None:
        subs = _build()
        first, second = subs.make_peers(), subs.make_peers()
        self.assertEqual(len(first), 2)
        for a, b in zip(first, second):
            with self.subTest(agent=a.name):
                self.assertIsNot(
                    a, b,
                    "make_peers returned the SAME instance twice — two graphs "
                    "would share tool/middleware objects",
                )

    def test_peers_are_not_the_orchestrators_instances(self) -> None:
        subs = _build()
        orchestrator = {id(sa) for sa in subs.sync_list}
        for peer in subs.make_peers():
            with self.subTest(agent=peer.name):
                self.assertNotIn(id(peer), orchestrator)

    def test_peers_exclude_general_purpose(self) -> None:
        """``build_agent_stack`` builds its own; a second would collide."""
        subs = _build()
        self.assertNotIn("general-purpose", [p.name for p in subs.make_peers()])

    def test_peers_share_the_task_runner(self) -> None:
        subs = _build()
        for peer in subs.make_peers():
            runner = getattr(peer, "_task_runner", None) or getattr(peer, "task_runner", None)
            if runner is not None:
                self.assertIs(runner, subs.task_runner)


class ExtractionIsComplete(unittest.TestCase):
    def test_build_daemon_no_longer_constructs_agents_inline(self) -> None:
        import inspect

        from yuyutsava.daemon import bootstrap

        src = inspect.getsource(bootstrap.build_daemon)
        # Includes the SECOND copy that lived at the ConversationManager call
        # site — five hand-maintained copies of one argument list before this.
        for cls in ("FileOrganizerAgent(", "FaceWatcherAgent(",
                    "GeneralPurposeAgent(", "make_tinker_subagent("):
            with self.subTest(construct=cls):
                self.assertNotIn(
                    cls, src,
                    f"build_daemon still constructs {cls} inline; the roster "
                    f"should come from build_subagents so it stays testable",
                )
        self.assertIn("build_subagents(", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
