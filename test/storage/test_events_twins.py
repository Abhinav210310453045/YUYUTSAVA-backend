"""Per-domain SQLite twins behind the Store facade — round-trip coverage.

Proves the monolithic-Store split preserved behaviour: events, proposals,
decisions, recall, counters, prefs, consent rules, and that the consent-grant
store still satisfies the *synchronous* ``ConsentStore`` Protocol via the
boot-loaded cache.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from yuyutsava.consent.models import Grant
from yuyutsava.consent.store import ConsentStore
from yuyutsava.storage.events import Store
from yuyutsava.storage.models import ConsentRule, Proposal


def _proposal(pid: str, *, event_id: str = "e1", status: str = "pending") -> Proposal:
    now = time.time()
    return Proposal(
        proposal_id=pid, event_id=event_id, topic="fs.write", summary="s",
        proposed="do", subagent="a", urgency=1, created_ts=now, expires_ts=now + 60,
        status=status, session_id=None, agent_path=None,
    )


class EventsTwinsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._tmp.name) / "state.db")
        await self.store.start()

    async def asyncTearDown(self) -> None:
        await self.store.stop()
        self._tmp.cleanup()

    async def test_event_payload_roundtrip(self) -> None:
        await self.store.put_event_payload(
            event_id="e1", topic="fs.write", ts=time.time(), payload={"path": "/x"}
        )
        rec = await self.store.get_event_payload("e1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.payload["path"], "/x")
        self.assertIsNone(await self.store.get_event_payload("missing"))

    async def test_proposal_status_flip_is_atomic(self) -> None:
        await self.store.put_proposal(_proposal("p1"))
        self.assertEqual((await self.store.get_proposal("p1")).status, "pending")
        first = await self.store.try_set_proposal_status(
            "p1", from_status="pending", to_status="approved"
        )
        second = await self.store.try_set_proposal_status(
            "p1", from_status="pending", to_status="approved"
        )
        self.assertTrue(first)
        self.assertFalse(second)  # already flipped — idempotent guard holds

    async def test_decisions_and_recall(self) -> None:
        await self.store.put_event_payload(
            event_id="e1", topic="fs.write", ts=time.time(), payload={}
        )
        await self.store.put_decision(
            proposal_id=None, event_id="e1", outcome="approved", action_summary="did it"
        )
        decisions = await self.store.list_decisions()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].outcome, "approved")
        hits = await self.store.recall("fs.*", since_sec=3600)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["topic"], "fs.write")
        self.assertEqual(await self.store.recall("net.*", since_sec=3600), [])

    async def test_tool_counters(self) -> None:
        day = "2026-06-21"
        self.assertEqual(await self.store.incr_tool_call("ws_search", day), 1)
        self.assertEqual(await self.store.incr_tool_call("ws_search", day), 2)
        self.assertEqual(await self.store.get_tool_call_count("ws_search", day), 2)
        self.assertEqual(await self.store.get_tool_call_count("other", day), 0)

    async def test_prefs(self) -> None:
        await self.store.put_pref("interaction.style", "terse")
        self.assertEqual(await self.store.get_pref("interaction.style"), "terse")
        self.assertEqual((await self.store.list_prefs())["interaction.style"], "terse")
        await self.store.delete_pref("interaction.style")
        self.assertIsNone(await self.store.get_pref("interaction.style"))

    async def test_consent_rules(self) -> None:
        await self.store.put_consent_rule(ConsentRule(
            rule_id="r1", topic_glob="fs.*", match_json="{}",
            decision="auto_approve", created_ts=time.time(), expires_ts=None,
        ))
        rules = await self.store.list_consent_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].decision, "auto_approve")

    async def test_consent_grant_store_satisfies_sync_protocol(self) -> None:
        # The facade must remain a structural ConsentStore (sync list).
        self.assertIsInstance(self.store, ConsentStore)
        g = Grant(
            grant_id="g1", domain="tool", subject_key="k", decision="allow",
            scope="project", scope_ref="*", created_ts=time.time(), expires_ts=None,
        )
        await self.store.put_consent_grant(g)
        self.assertTrue(any(x.grant_id == "g1" for x in self.store.list_consent_grants()))
        # Cache survives a reopen (boot preload), and delete clears it.
        await self.store.stop()
        await self.store.start()
        self.assertTrue(any(x.grant_id == "g1" for x in self.store.list_consent_grants()))
        await self.store.delete_consent_grant("g1")
        self.assertFalse(any(x.grant_id == "g1" for x in self.store.list_consent_grants()))


if __name__ == "__main__":
    unittest.main()
