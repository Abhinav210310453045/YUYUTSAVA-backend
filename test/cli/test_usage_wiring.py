"""CLI runs record their token usage, and the REPL can report it.

``build_agent_stack`` has taken a ``usage_store`` kwarg since the daemon
needed one, but nothing filled it in standalone mode — so a CLI session wrote
zero ``llm_usage`` rows and "what did that cost?" was unanswerable from
inside a session. The 12 Sep post-mortem had to reconstruct 4M input tokens
from ``usage_metadata`` on recorded messages, which only worked because the
transcript store happened to be on.

Two things have to hold for the rows to be useful: they must be written, and
they must carry the thread, since a chat bundle is shared across conversations
and cannot pin one at build time.

Run:  .venv/bin/python test/cli/test_usage_wiring.py
"""

from __future__ import annotations

import asyncio
import inspect
import time
import unittest

import yuyutsava.core.config  # noqa: F401 — import first (core/__init__ cycle)
from yuyutsava.cli import agent_stack
from yuyutsava.cli.commands.chat_repl import (
    _fmt_usage,
    _print_usage_summary,
    _usage_rows,
)
from yuyutsava.core.engine import AgentBundle
from yuyutsava.daemon.usage import UsagePolicy, UsageRow
from yuyutsava.policy.types import Turn, Usage


class FakeUsageStore:
    def __init__(self, rows: list[UsageRow] | None = None) -> None:
        self.rows = list(rows or [])
        self.added: list[UsageRow] = []

    async def add(self, row: UsageRow) -> None:
        self.added.append(row)

    async def list(self, *, task_id=None, thread_id=None, since=None, limit=200):
        # Mirrors the real store's filters, thread_id included — the REPL asks
        # the store to narrow by conversation now rather than doing it itself.
        return [
            r for r in self.rows
            if (since is None or r.ts >= since)
            and (not thread_id or r.thread_id == thread_id)
            and (not task_id or r.task_id == task_id)
        ][:limit]

    async def aggregate(self, *, since=None, group_by=None):
        return []


def row(thread="T", *, tin=1000, tout=10, cost=0.001, model="gemini-3.5-flash",
        ts=None, cached=0):
    return UsageRow(
        id=f"usg_{tin}", ts=ts if ts is not None else time.time(), thread_id=thread,
        task_id="", role="cli", model=model, input_tokens=tin, output_tokens=tout,
        est_cost_usd=cost, cache_read_tokens=cached,
    )


class StackWiresAUsageStore(unittest.TestCase):
    def test_standalone_builds_its_own_store(self):
        src = inspect.getsource(agent_stack.build_agent_stack)
        self.assertIn("if usage_store is None:", src)
        self.assertIn("_factory.usage()", src)

    def test_store_reaches_the_bundle_for_reporting(self):
        src = inspect.getsource(agent_stack.build_agent_stack)
        self.assertIn("bundle.usage_store = usage_store", src)
        self.assertIn("usage_store", {f.name for f in AgentBundle.__dataclass_fields__.values()})

    def test_bundle_default_is_none(self):
        # A bundle built by any other path must not pretend to have one.
        self.assertIsNone(AgentBundle.__dataclass_fields__["usage_store"].default)


class PolicyTagsTheThread(unittest.IsolatedAsyncioTestCase):
    async def test_unpinned_policy_takes_the_threads_id_from_the_turn(self):
        store = FakeUsageStore()
        policy = UsagePolicy(store, role="cli", model_name="gemini-3.5-flash")
        turn = Turn(thread_id="cli-abc", usage=Usage(input_tokens=41_087, output_tokens=64))

        await policy.after_model(turn)

        self.assertEqual(len(store.added), 1)
        self.assertEqual(store.added[0].thread_id, "cli-abc")
        self.assertEqual(store.added[0].input_tokens, 41_087)

    async def test_a_pinned_thread_still_wins(self):
        # The orchestrator pins at build time; that must not be overridden by
        # whatever thread the call happens to run on.
        store = FakeUsageStore()
        policy = UsagePolicy(store, role="orchestrator", thread_id="pinned")
        await policy.after_model(Turn(thread_id="other", usage=Usage(input_tokens=5, output_tokens=1)))
        self.assertEqual(store.added[0].thread_id, "pinned")

    async def test_a_call_with_no_reported_usage_is_not_recorded(self):
        # A zero row is indistinguishable from a genuinely free call.
        store = FakeUsageStore()
        policy = UsagePolicy(store, role="cli")
        await policy.after_model(Turn(thread_id="t", usage=None))
        await policy.after_model(Turn(thread_id="t", usage=Usage()))
        self.assertEqual(store.added, [])

    async def test_a_store_failure_never_fails_the_turn(self):
        class Broken(FakeUsageStore):
            async def add(self, row):
                raise RuntimeError("db down")

        policy = UsagePolicy(Broken(), role="cli")
        self.assertIsNone(
            await policy.after_model(Turn(thread_id="t", usage=Usage(input_tokens=1)))
        )


class Reporting(unittest.TestCase):
    def test_footer_shape(self):
        out = _fmt_usage([row(tin=41_087, tout=64, cost=0.0032), row(tin=43_617, tout=79, cost=0.0034)])
        self.assertEqual(out, "2 calls · in 84,704 · out 143 · ~$0.0066")

    def test_singular_call(self):
        self.assertTrue(_fmt_usage([row()]).startswith("1 call ·"))

    def test_unknown_price_says_nothing_rather_than_zero(self):
        # "$0.00" would read as free; a missing price table entry is not free.
        self.assertNotIn("$", _fmt_usage([row(cost=0.0)]))

    def test_nothing_to_report_is_empty(self):
        self.assertEqual(_fmt_usage([]), "")

    def test_rows_are_filtered_to_this_thread(self):
        store = FakeUsageStore([row("T"), row("OTHER"), row("T")])
        got = asyncio.run(_usage_rows(store, "T", 0))
        self.assertEqual(len(got), 2)

    def test_the_thread_filter_is_pushed_down_to_the_store(self):
        # Not a detail: filtering in Python meant reading a fixed 2,000-row
        # window first, so on a busy machine a session's own rows could fall
        # off the end and the footer would under-report.
        seen = {}

        class Recording(FakeUsageStore):
            async def list(self, **kw):
                seen.update(kw)
                return []

        asyncio.run(_usage_rows(Recording(), "T", 0))
        self.assertEqual(seen.get("thread_id"), "T")

    def test_cache_share_is_shown_when_the_provider_reported_one(self):
        out = _fmt_usage([row(tin=1_000, cached=930)])
        self.assertIn("↺93%", out)

    def test_no_cache_detail_shows_no_share_rather_than_zero_percent(self):
        # 0 means "the provider reported no cache detail", which is not the
        # same claim as a 0 % hit rate.
        self.assertNotIn("↺", _fmt_usage([row(tin=1_000, cached=0)]))

    def test_time_window_narrows_the_read(self):
        now = time.time()
        store = FakeUsageStore([row("T", ts=now - 600), row("T", ts=now)])
        self.assertEqual(len(asyncio.run(_usage_rows(store, "T", now - 60))), 1)

    def test_missing_or_broken_store_is_silent(self):
        class Broken(FakeUsageStore):
            async def list(self, **k):
                raise RuntimeError("down")

        self.assertEqual(asyncio.run(_usage_rows(None, "T", 0)), [])
        self.assertEqual(asyncio.run(_usage_rows(FakeUsageStore(), "", 0)), [])
        self.assertEqual(asyncio.run(_usage_rows(Broken(), "T", 0)), [])

    def test_summary_runs_on_empty_and_populated_stores(self):
        # Output goes to stderr; this pins that neither path raises.
        asyncio.run(_print_usage_summary(FakeUsageStore(), "T", 0))
        asyncio.run(
            _print_usage_summary(
                FakeUsageStore([row("T", model="a"), row("T", model="b")]), "T", 0
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
