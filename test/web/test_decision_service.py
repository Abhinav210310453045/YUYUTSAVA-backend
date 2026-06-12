"""DecisionService unit tests + HTTP regression for the Phase-3 extraction.

Run:  uv run python -m unittest test.web.test_decision_service -v
"""

from __future__ import annotations

import asyncio
import unittest

import httpx

from yuyutsava.daemon.channels import ProposalDecision
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.services.decision_service import (
    DecisionConflictError,
    DecisionService,
)
from yuyutsava.daemon.web.services.stream_service import WebHub


class _FakeStore:
    """Only the surface DecisionService touches."""

    def __init__(self, *, flip_ok: bool = True) -> None:
        self.flip_ok = flip_ok
        self.flips: list[tuple[str, str, str]] = []

    def try_set_proposal_status(
        self, proposal_id: str, *, from_status: str, to_status: str,
    ) -> bool:
        self.flips.append((proposal_id, from_status, to_status))
        return self.flip_ok


class DecisionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_future_in_any_registered_map(self) -> None:
        store = _FakeStore()
        service = DecisionService(store)
        web_props: dict = {}
        plugin_props: dict = {}
        service.add_waiters(proposals=web_props, asks={})
        service.add_waiters(proposals=plugin_props, asks={})

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        plugin_props["prop_1"] = fut

        outcome = await service.respond_proposal("prop_1", "approve")
        self.assertTrue(outcome.ok)
        self.assertIsNone(outcome.note)
        self.assertEqual(
            fut.result(), ProposalDecision(decision="approve", edited_instruction=None),
        )
        self.assertEqual(store.flips, [("prop_1", "pending", "approved")])

    async def test_modify_carries_edited_instruction(self) -> None:
        service = DecisionService(_FakeStore())
        props: dict = {}
        service.add_waiters(proposals=props, asks={})
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        props["p2"] = fut
        await service.respond_proposal("p2", "modify", edited_instruction="do Y instead")
        self.assertEqual(fut.result().edited_instruction, "do Y instead")

    async def test_non_modify_drops_edited_instruction(self) -> None:
        service = DecisionService(_FakeStore())
        props: dict = {}
        service.add_waiters(proposals=props, asks={})
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        props["p3"] = fut
        await service.respond_proposal("p3", "skip", edited_instruction="ignored")
        self.assertIsNone(fut.result().edited_instruction)

    async def test_conflict_when_not_pending(self) -> None:
        service = DecisionService(_FakeStore(flip_ok=False))
        with self.assertRaises(DecisionConflictError):
            await service.respond_proposal("gone", "approve")

    async def test_invalid_decision_rejected(self) -> None:
        service = DecisionService(_FakeStore())
        with self.assertRaises(ValueError):
            await service.respond_proposal("p", "expired")

    async def test_no_listener_note(self) -> None:
        service = DecisionService(_FakeStore())
        outcome = await service.respond_proposal("orphan", "approve")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.note, "no listener (already resolved)")

    async def test_respond_ask_paths(self) -> None:
        service = DecisionService(_FakeStore())
        asks: dict = {}
        service.add_waiters(proposals={}, asks=asks)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        asks["ask_1"] = fut
        await service.respond_ask("ask_1", "   ")  # blank → reject
        self.assertEqual(fut.result(), "reject")
        with self.assertRaises(DecisionConflictError):
            await service.respond_ask("ask_missing", "yes")

    async def test_pending_ids_across_maps(self) -> None:
        service = DecisionService(_FakeStore())
        loop = asyncio.get_running_loop()
        m1, m2 = {"a": loop.create_future()}, {"b": loop.create_future()}
        a1 = {"x": loop.create_future()}
        service.add_waiters(proposals=m1, asks=a1)
        service.add_waiters(proposals=m2, asks={})
        m2["done"] = loop.create_future()
        m2["done"].set_result(ProposalDecision(decision="skip"))
        props, asks = service.pending_ids()
        self.assertEqual(sorted(props), ["a", "b"])
        self.assertEqual(asks, ["x"])

    async def test_duplicate_add_waiters_is_noop(self) -> None:
        service = DecisionService(_FakeStore())
        props: dict = {}
        service.add_waiters(proposals=props, asks={})
        service.add_waiters(proposals=props, asks={})
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        props["p"] = fut
        ids, _ = service.pending_ids()
        self.assertEqual(ids, ["p"])  # not double-counted


class HttpRegressionTests(unittest.IsolatedAsyncioTestCase):
    """The extracted endpoints must behave exactly as before Phase 3."""

    async def asyncSetUp(self) -> None:
        self.store = _FakeStore()
        self.hub = WebHub(store=self.store)
        app = create_app(self.hub, host="127.0.0.1")
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_proposal_respond_resolves_hub_future(self) -> None:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.hub.pending_proposals["p1"] = fut
        r = await self.client.post(
            "/proposal/p1/respond", json={"decision": "approve"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(fut.result().decision, "approve")

    async def test_proposal_conflict_409(self) -> None:
        self.store.flip_ok = False
        r = await self.client.post(
            "/proposal/p1/respond", json={"decision": "approve"},
        )
        self.assertEqual(r.status_code, 409)

    async def test_ask_respond(self) -> None:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.hub.pending_asks["a1"] = fut
        r = await self.client.post("/ask/a1/respond", json={"response": "approve"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(fut.result(), "approve")

        r = await self.client.post("/ask/a2/respond", json={"response": "x"})
        self.assertEqual(r.status_code, 409)


if __name__ == "__main__":
    unittest.main()
