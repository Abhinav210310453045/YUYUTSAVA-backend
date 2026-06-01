"""Unit tests for the async-subagents stack (no network, no LLM).

Runnable as a script (``uv run python test/async_subagents/test_unit.py``)
or via pytest. Exits non-zero on first failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from dataclasses import dataclass

# Make repo root importable when run as a script.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from yuyutsava.async_subagents.mirror import (
    AsyncTaskMirror,
    MirroredTask,
    TERMINAL_STATUSES,
)
from yuyutsava.async_subagents.remote import RemoteAsyncSubagentSpec
from yuyutsava.async_subagents.session_origin import SessionOriginMap
from yuyutsava.agents.orchestrator.capabilities import render_capabilities_block
from yuyutsava.daemon.channels import (
    AskPrompt,
    ChannelEvent,
    ChannelRouter,
    LogPayload,
    ProposalDecision,
    UserChannel,
)


def _now() -> float:
    return time.time()


def _mk_task(task_id: str, status: str = "running", agent_name: str = "x-bg") -> MirroredTask:
    return MirroredTask(
        task_id=task_id,
        agent_name=agent_name,
        graph_id=agent_name.removesuffix("-bg"),
        instruction="t",
        status=status,
        started_at=_now() - 5,
        last_update_at=_now(),
    )


# ---------------------------------------------------------------------------
# AsyncTaskMirror
# ---------------------------------------------------------------------------


class MirrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_upsert_then_list(self):
        m = AsyncTaskMirror()
        await m.upsert(_mk_task("a", status="running"))
        await m.upsert(_mk_task("b", status="awaiting_user"))
        self.assertEqual(len(m.list_all()), 2)
        self.assertEqual(m.count_running(), 2)

    async def test_terminal_status_drops_from_running_count(self):
        m = AsyncTaskMirror()
        await m.upsert(_mk_task("a", status="running"))
        await m.set_status("a", "success", summary="done")
        t = m.get("a")
        self.assertIn(t.status, TERMINAL_STATUSES)
        self.assertEqual(m.count_running(), 0)
        self.assertEqual(t.summary, "done")
        self.assertIsNotNone(t.completed_at)

    async def test_set_status_missing(self):
        m = AsyncTaskMirror()
        self.assertIsNone(await m.set_status("nope", "success"))

    async def test_render_block_empty(self):
        m = AsyncTaskMirror()
        self.assertEqual(m.render_block(), "")

    async def test_render_block_format(self):
        m = AsyncTaskMirror()
        await m.upsert(_mk_task("alpha9999", status="running", agent_name="file-organizer-bg"))
        await m.upsert(_mk_task("beta5555", status="awaiting_user", agent_name="face-watcher-bg"))
        block = m.render_block()
        self.assertTrue(block.startswith("Background tasks in flight:"))
        self.assertIn("file-organizer-bg", block)
        self.assertIn("face-watcher-bg", block)
        self.assertIn("status=running", block)
        self.assertIn("status=awaiting_user", block)

    async def test_render_block_truncates(self):
        m = AsyncTaskMirror()
        for i in range(15):
            await m.upsert(_mk_task(f"t{i:04d}", agent_name=f"x{i}-bg"))
        block = m.render_block(max_lines=5)
        self.assertIn("…and 10 more.", block)

    async def test_mark_all_cancelled(self):
        m = AsyncTaskMirror()
        await m.upsert(_mk_task("a", status="running"))
        await m.upsert(_mk_task("b", status="awaiting_user"))
        await m.upsert(_mk_task("c", status="success"))
        cancelled = list(await m.mark_all_cancelled(reason="shutdown"))
        self.assertEqual(len(cancelled), 2)
        self.assertEqual(m.count_running(), 0)
        self.assertEqual(m.get("a").status, "cancelled")
        self.assertEqual(m.get("c").status, "success")  # already terminal, untouched


# ---------------------------------------------------------------------------
# SessionOriginMap + ChannelRouter
# ---------------------------------------------------------------------------


class _FakeChannel(UserChannel):
    def __init__(self, name: str):
        self.name = name
        self.asks_received: list[str] = []
        self.raise_not_implemented: bool = False

    async def post_event(self, ev):  # noqa: D401, ANN001
        pass

    async def post_proposal(self, p):  # noqa: D401, ANN001
        return ProposalDecision(decision="approve")

    async def post_ask(self, a):  # noqa: D401, ANN001
        if self.raise_not_implemented:
            raise NotImplementedError
        self.asks_received.append(a.ask_id)
        return f"{self.name}_response"


def _ask(ask_id: str, session_id: str | None = None) -> AskPrompt:
    return AskPrompt(
        ask_id=ask_id, title="t", body="b", options=["ok"],
        interrupt_value={}, session_id=session_id,
    )


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_first_available_picks_primary(self):
        web = _FakeChannel("web")
        term = _FakeChannel("terminal")
        r = ChannelRouter(channels=[term, web], primary_name="web")
        reply = await r.post_ask(_ask("a"))
        self.assertEqual(reply, "web_response")

    async def test_origin_overrides_primary(self):
        web = _FakeChannel("web")
        cli = _FakeChannel("cli-remote")
        om = SessionOriginMap()
        om.set("sess-1", "cli-remote")
        r = ChannelRouter(channels=[web, cli], primary_name="web", session_origin=om)
        reply = await r.post_ask(_ask("a", session_id="sess-1"))
        self.assertEqual(reply, "cli-remote_response")
        # Unknown session falls back to primary.
        reply2 = await r.post_ask(_ask("b", session_id="sess-unknown"))
        self.assertEqual(reply2, "web_response")

    async def test_origin_channel_disconnected_falls_through(self):
        web = _FakeChannel("web")
        om = SessionOriginMap()
        om.set("sess-1", "cli-remote")  # but cli-remote isn't in channels
        r = ChannelRouter(channels=[web], primary_name="web", session_origin=om)
        reply = await r.post_ask(_ask("a", session_id="sess-1"))
        self.assertEqual(reply, "web_response")

    async def test_not_implemented_skips_to_next(self):
        web = _FakeChannel("web")
        web.raise_not_implemented = True
        term = _FakeChannel("terminal")
        r = ChannelRouter(channels=[term, web], primary_name="web")
        reply = await r.post_ask(_ask("a"))
        self.assertEqual(reply, "terminal_response")

    async def test_no_channels_returns_reject(self):
        r = ChannelRouter(channels=[], primary_name="web")
        reply = await r.post_ask(_ask("a"))
        self.assertEqual(reply, "reject")

    async def test_post_event_fans_out(self):
        a = _FakeChannel("a")
        b = _FakeChannel("b")
        r = ChannelRouter(channels=[a, b])
        await r.post_event(ChannelEvent(payload=LogPayload(text="hi")))
        # No raises — both channels received.


# ---------------------------------------------------------------------------
# RemoteAsyncSubagentSpec
# ---------------------------------------------------------------------------


class RemoteSpecTests(unittest.TestCase):
    def test_round_trip_without_headers(self):
        r = RemoteAsyncSubagentSpec(
            name="researcher-bg",
            description="off-process research",
            graph_id="researcher",
            url="https://r.example.com/",
        )
        spec = r.as_async_subagent_spec()
        self.assertEqual(spec["name"], "researcher-bg")
        self.assertEqual(spec["graph_id"], "researcher")
        self.assertEqual(spec["url"], "https://r.example.com/")
        self.assertNotIn("headers", spec)

    def test_round_trip_with_headers(self):
        r = RemoteAsyncSubagentSpec(
            name="x", description="d", graph_id="g", url="u",
            headers={"Authorization": "Bearer xyz"},
        )
        spec = r.as_async_subagent_spec()
        self.assertEqual(spec["headers"], {"Authorization": "Bearer xyz"})
        # Mutating the returned dict's headers must not affect the spec.
        spec["headers"]["Authorization"] = "tampered"
        self.assertEqual(r.headers["Authorization"], "Bearer xyz")


# ---------------------------------------------------------------------------
# Capabilities block
# ---------------------------------------------------------------------------


@dataclass
class _FakeSub:
    name: str
    description: str
    supports_async: bool = True

    def async_subagent_name(self) -> str:
        return f"{self.name}-bg"


class CapabilitiesTests(unittest.TestCase):
    def test_empty(self):
        self.assertIn("(no subagents", render_capabilities_block([]))

    def test_sync_only(self):
        block = render_capabilities_block([_FakeSub("file-organizer", "Organizes files")])
        self.assertIn("file-organizer [sync]", block)
        self.assertNotIn("[background", block)

    def test_async_includes_start_hint(self):
        block = render_capabilities_block(
            [_FakeSub("file-organizer", "Organizes files")],
            async_subagents=[_FakeSub("file-organizer", "Organizes files")],
        )
        self.assertIn("file-organizer [sync]", block)
        self.assertIn("file-organizer-bg [background, local]", block)
        self.assertIn("start_async_task(subagent_type='file-organizer-bg'", block)

    def test_remote_tagged(self):
        remote = RemoteAsyncSubagentSpec(
            name="research-bg",
            description="off-process research",
            graph_id="research",
            url="https://r.example.com/",
        )
        block = render_capabilities_block([], remote_async_subagents=[remote])
        self.assertIn("research-bg [background, remote]", block)
        self.assertIn("off-process research", block)

    def test_async_skips_subagents_that_opt_out(self):
        opted_out = _FakeSub("x", "d", supports_async=False)
        block = render_capabilities_block([], async_subagents=[opted_out])
        self.assertNotIn("x-bg", block)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    unittest.main(verbosity=2)
