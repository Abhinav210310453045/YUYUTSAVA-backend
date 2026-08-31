"""Origin-aware HITL routing: channel-origin tasks map thread→channel.

Run:  uv run python -m unittest test.daemon.test_orchestrator_session_origin -v
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass

from yuyutsava.async_subagents.session_origin import SessionOriginMap
from yuyutsava.daemon.channels import ChannelEvent, ChannelRouter, UserChannel
from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop


@dataclass
class _Rec:
    origin: str


class _FakeRegistry:
    def __init__(self, origin: str) -> None:
        self._origin = origin

    @staticmethod
    def mint_task_id() -> str:
        return "tsk_fake"

    async def get(self, task_id: str) -> _Rec:
        return _Rec(origin=self._origin)


class _NamedChannel(UserChannel):
    def __init__(self, name: str) -> None:
        self.name = name

    async def post_event(self, ev: ChannelEvent) -> None: ...

    async def post_proposal(self, p):
        raise NotImplementedError

    async def post_ask(self, a):
        raise NotImplementedError


def make_loop(origin: str, *, with_channel: bool, with_origin_map: bool = True):
    router = ChannelRouter(channels=[])
    if with_origin_map:
        router.session_origin = SessionOriginMap()
    if with_channel:
        router.register(_NamedChannel(origin))
    loop = OrchestratorLoop(
        task_queue=asyncio.Queue(),
        channels=router,
        store=None,                      # not touched by _map_session_origin
        orchestrator_model=None,
        deps=None,
        orchestrator_token_budget=0,
        task_registry=_FakeRegistry(origin),
    )
    return loop, router


class SessionOriginMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_origin_is_mapped(self) -> None:
        loop, router = make_loop("telegram", with_channel=True)
        mapped = await loop._map_session_origin("tsk_1", "thr_1")
        self.assertEqual(mapped, "telegram")
        self.assertEqual(router.session_origin.get("thr_1"), "telegram")

    async def test_non_channel_origin_not_mapped(self) -> None:
        loop, router = make_loop("api", with_channel=False)
        mapped = await loop._map_session_origin("tsk_1", "thr_1")
        self.assertIsNone(mapped)
        self.assertIsNone(router.session_origin.get("thr_1"))

    async def test_no_origin_map_is_safe(self) -> None:
        loop, _router = make_loop("telegram", with_channel=True, with_origin_map=False)
        self.assertIsNone(await loop._map_session_origin("tsk_1", "thr_1"))

    async def test_blank_task_id_is_safe(self) -> None:
        loop, router = make_loop("telegram", with_channel=True)
        self.assertIsNone(await loop._map_session_origin("", "thr_1"))
        self.assertIsNone(router.session_origin.get("thr_1"))


if __name__ == "__main__":
    unittest.main()
