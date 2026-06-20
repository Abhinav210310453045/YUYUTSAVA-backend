"""Resolved-broadcast symmetry across surfaces.

Regression for the bug where a CLI-launched background-task approval, answered
from any surface, resumed the agent but never cleared the prompt: ``WebChannel``
broadcast ``ask_resolved`` / ``proposal_resolved`` in its ``finally`` but
``CliRemoteChannel`` did not. Both now route through the shared ``WebHub``
helpers, so this asserts both channels emit the resolution item.

Run:  uv run python -m unittest test.web.test_channel_resolve_broadcast -v
"""

from __future__ import annotations

import asyncio
import time
import unittest

from yuyutsava.daemon.channels import AskPrompt
from yuyutsava.daemon.cli_remote_channel import CliRemoteChannel
from yuyutsava.daemon.web.services.stream_service import (
    StreamAskItem,
    StreamAskResolvedItem,
    StreamProposalItem,
    StreamProposalResolvedItem,
    WebChannel,
    WebHub,
)


class _FakeStore:
    def try_set_proposal_status(self, *a, **k) -> bool:  # noqa: D401, ANN002
        return True


class _FakeProposal:
    """Only the attributes ``post_proposal`` reads (no serialization here)."""

    def __init__(self, pid: str, session_id: str | None = None, ttl: float = 30.0):
        self.proposal_id = pid
        self.session_id = session_id
        self.expires_ts = time.time() + ttl


class ResolveBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hub = WebHub(store=_FakeStore())
        self.items: list = []
        self._sub = asyncio.create_task(self._drain())
        # Let the subscriber register its queue before any broadcast.
        for _ in range(50):
            await asyncio.sleep(0)
            if self.hub._subscribers:  # noqa: SLF001 — test introspection
                break

    async def asyncTearDown(self) -> None:
        self._sub.cancel()
        try:
            await self._sub
        except asyncio.CancelledError:
            pass

    async def _drain(self) -> None:
        async for item in self.hub.subscribe():
            self.items.append(item)

    async def _wait_pending_ask(self, ask_id: str) -> None:
        for _ in range(200):
            if ask_id in self.hub.pending_asks:
                return
            await asyncio.sleep(0.005)
        self.fail(f"ask {ask_id} never registered a pending future")

    async def _wait_pending_proposal(self, pid: str) -> None:
        for _ in range(200):
            if pid in self.hub.pending_proposals:
                return
            await asyncio.sleep(0.005)
        self.fail(f"proposal {pid} never registered a pending future")

    def _types(self) -> list[str]:
        return [type(i).__name__ for i in self.items]

    async def _assert_ask_flow(self, channel) -> None:
        a = AskPrompt(
            ask_id="ask_x", title="t", body="b",
            options=["yes", "no"], interrupt_value={}, session_id="s1",
        )
        post = asyncio.create_task(channel.post_ask(a))
        await self._wait_pending_ask("ask_x")
        self.hub.pending_asks["ask_x"].set_result("approve")
        self.assertEqual(await post, "approve")
        await asyncio.sleep(0.01)

        self.assertIn(StreamAskItem.__name__, self._types())
        resolved = [i for i in self.items if isinstance(i, StreamAskResolvedItem)]
        self.assertTrue(resolved, "no ask_resolved broadcast")
        self.assertEqual(resolved[-1].ask_id, "ask_x")
        self.assertEqual(resolved[-1].session_id, "s1")
        self.assertNotIn("ask_x", self.hub.pending_asks)

    async def _assert_proposal_flow(self, channel) -> None:
        from yuyutsava.daemon.channels import ProposalDecision

        p = _FakeProposal("prop_x", session_id="s2")
        post = asyncio.create_task(channel.post_proposal(p))
        await self._wait_pending_proposal("prop_x")
        self.hub.pending_proposals["prop_x"].set_result(
            ProposalDecision(decision="approve")
        )
        out = await post
        self.assertEqual(out.decision, "approve")
        await asyncio.sleep(0.01)

        self.assertIn(StreamProposalItem.__name__, self._types())
        resolved = [i for i in self.items if isinstance(i, StreamProposalResolvedItem)]
        self.assertTrue(resolved, "no proposal_resolved broadcast")
        self.assertEqual(resolved[-1].proposal_id, "prop_x")
        self.assertNotIn("prop_x", self.hub.pending_proposals)

    async def test_cli_remote_channel_broadcasts_ask_resolved(self) -> None:
        await self._assert_ask_flow(CliRemoteChannel(self.hub))

    async def test_cli_remote_channel_broadcasts_proposal_resolved(self) -> None:
        await self._assert_proposal_flow(CliRemoteChannel(self.hub))

    async def test_web_channel_still_broadcasts_ask_resolved(self) -> None:
        await self._assert_ask_flow(WebChannel(self.hub))


if __name__ == "__main__":
    unittest.main()
