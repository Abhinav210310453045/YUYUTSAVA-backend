"""Losslessness: everything the agent has seen stays reachable.

Offloading and compaction both remove content from the prompt. Neither may
remove it from the *world* — the agent must always have a tool that can bring
it back, and no read may itself be truncated into a dead end. The measured
failure modes this pins down, all found in the 12 Sep session post-mortem:

* an unbounded ``ctx_fetch_artifact(length=…)`` could exceed
  ``LIMITS.max_tool_result_chars``, and ``guard_tool_result`` would then
  replace the body with a "too large" notice — the one truncation that loses
  data instead of deferring it, and it hit the very tool whose job is recovery;
* ``task`` and ``tool_search`` were exempt from offload *at every size*, so a
  huge subagent response met that same guard with nothing stored behind it;
* compaction dropped the evicted turns with no addressable way back, leaving
  the summary as the only trace;
* bulky ``content=`` arguments were re-sent on every later call forever.

Run:  .venv/bin/python test/context/test_lossless_readback.py
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import yuyutsava.core.config  # noqa: F401 — import first (core/__init__ cycle)
from yuyutsava.context.artifacts import (
    MAX_SLICE_CHARS,
    ArtifactSlice,
    clamp_slice_length,
)
from yuyutsava.context.config import ContextSettings
from yuyutsava.context.offload_policy import ToolResultOffloadPolicy
from yuyutsava.context.tools import make_context_tools
from yuyutsava.core.config import LIMITS
from yuyutsava.core.tool_result import guard_tool_result
from yuyutsava.policy.types import ToolCall

THREAD = "thread-under-test"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeArtifactStore:
    """In-memory ArtifactStore with the real shared grep/read_lines behaviour."""

    supports_recall = False

    def __init__(self, bodies: dict[str, str] | None = None) -> None:
        self.bodies = dict(bodies or {})
        self.puts: list[tuple[str, str, int]] = []

    async def put(self, thread_id: str, tool_name: str, content: str) -> str:
        aid = f"art_{len(self.bodies)}"
        self.bodies[aid] = content
        self.puts.append((thread_id, tool_name, len(content)))
        return aid

    async def get(self, artifact_id, offset=0, length=20_000):
        body = self.bodies.get(artifact_id)
        if body is None:
            return None
        total = len(body)
        offset = max(0, offset)
        content = body[offset:] if length < 0 else body[offset : offset + max(0, length)]
        return ArtifactSlice(
            artifact_id=artifact_id, content=content, offset=offset, total_chars=total
        )

    async def delete_older_than(self, cutoff_ts: float) -> int:
        return 0

    # grep() and read_lines() come from the ABC's shared implementation.
    grep = ToolResultOffloadPolicy.__mro__ and None  # placeholder, replaced below


# Reuse the real shared implementations rather than reimplementing them here:
# that is the code under test for line addressing.
from yuyutsava.context.artifacts import ArtifactStore as _RealStore  # noqa: E402

FakeArtifactStore.grep = _RealStore.grep
FakeArtifactStore.read_lines = _RealStore.read_lines


class FakeTranscriptStore:
    """TranscriptStore with `seq > after_seq` paging, matching the real one."""

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    async def list_messages(self, thread_id, *, after_seq=0, limit=1000):
        if thread_id != THREAD:
            return []
        out = [r for r in self.rows if r.seq > after_seq]
        return out[:limit]


class Rec:
    """Minimal TranscriptMessage stand-in."""

    def __init__(self, seq: int, type_: str, data: dict) -> None:
        self.seq = seq
        self.type = type_
        self.content = {"type": type_, "data": data}


def tools_for(store, transcripts=None) -> dict[str, Any]:
    return {t.name: t for t in make_context_tools(store, transcripts)}


def run(coro):
    return asyncio.run(coro)


def with_thread(fn):
    """Run *fn* with ``thread_id_from_runtime`` resolving to THREAD."""
    with mock.patch(
        "yuyutsava.context.tools.thread_id_from_runtime", return_value=THREAD
    ):
        return fn()


# ---------------------------------------------------------------------------
# 1. No ctx_ read can reach the size guard
# ---------------------------------------------------------------------------


class ClampedReads(unittest.TestCase):
    def test_clamp_bounds_agent_input_but_not_internal_reads(self):
        self.assertEqual(clamp_slice_length(200_000), MAX_SLICE_CHARS)
        self.assertEqual(clamp_slice_length(500), 500)
        # 0/negative mean "unset" at the tool layer → the default window.
        self.assertEqual(clamp_slice_length(0), 20_000)
        self.assertEqual(clamp_slice_length(-1), 20_000)

    def test_ceiling_is_below_the_guard(self):
        # The whole point: a clamped read plus its header can never be the
        # thing guard_tool_result suppresses.
        self.assertLess(MAX_SLICE_CHARS, LIMITS.max_tool_result_chars)

    def test_oversized_request_returns_a_slice_and_a_continuation(self):
        body = "x" * 250_000
        store = FakeArtifactStore({"art_big": body})
        fetch = tools_for(store)["ctx_fetch_artifact"]

        out = run(fetch.ainvoke({"artifact_id": "art_big", "length": 10_000_000}))

        self.assertLessEqual(len(out), MAX_SLICE_CHARS + 200)  # + header/footer
        self.assertIn("[more: call ctx_fetch_artifact", out)
        self.assertIn(f"of {len(body)}", out)
        # And the guard leaves it alone, which is the property that matters.
        self.assertIs(guard_tool_result(out, "ctx_fetch_artifact"), out)

    def test_paging_reaches_the_end_without_loss(self):
        body = "".join(f"line {i}\n" for i in range(12_000))
        store = FakeArtifactStore({"art_big": body})
        fetch = tools_for(store)["ctx_fetch_artifact"]

        seen, offset, guard = [], 0, 0
        while True:
            guard += 1
            self.assertLess(guard, 50, "paging did not terminate")
            out = run(
                fetch.ainvoke({"artifact_id": "art_big", "offset": offset, "length": 50_000})
            )
            chunk = out.split("\n", 1)[1]
            more = "[more: call ctx_fetch_artifact" in chunk
            if more:
                chunk = chunk.rsplit("\n[more:", 1)[0]
            seen.append(chunk)
            offset += len(chunk)
            if not more:
                break
        self.assertEqual("".join(seen), body)  # every character recovered


# ---------------------------------------------------------------------------
# 2. Line addressing composes with grep
# ---------------------------------------------------------------------------


class LineAddressing(unittest.TestCase):
    def setUp(self):
        self.body = "".join(f"row {i}: value-{i}\n" for i in range(1, 1001))
        self.store = FakeArtifactStore({"art_l": self.body})
        self.tools = tools_for(self.store)

    def test_grep_hit_can_be_widened_by_line(self):
        hits = run(
            self.tools["ctx_grep_artifact"].ainvoke(
                {"artifact_id": "art_l", "pattern": r"value-812$"}
            )
        )
        self.assertIn("812: row 812", hits)
        lineno = int(hits.splitlines()[1].split(":", 1)[0])

        out = run(
            self.tools["ctx_fetch_artifact"].ainvoke(
                {"artifact_id": "art_l", "start_line": lineno - 2, "line_count": 5}
            )
        )
        self.assertIn("[artifact art_l lines 810-814 of 1000]", out)
        self.assertIn("row 812: value-812", out)
        self.assertIn("[more: call ctx_fetch_artifact(artifact_id, start_line=815)]", out)

    def test_last_page_has_no_continuation(self):
        out = run(
            self.tools["ctx_fetch_artifact"].ainvoke(
                {"artifact_id": "art_l", "start_line": 996, "line_count": 50}
            )
        )
        self.assertIn("lines 996-1000 of 1000", out)
        self.assertNotIn("[more:", out)

    def test_a_single_giant_line_is_prefixed_and_redirected_to_char_paging(self):
        # Advancing to start_line=2 here would skip the rest of line 1, so the
        # footer must send the agent to character paging instead.
        store = FakeArtifactStore({"art_g": "y" * (MAX_SLICE_CHARS * 3)})
        out = run(
            tools_for(store)["ctx_fetch_artifact"].ainvoke(
                {"artifact_id": "art_g", "start_line": 1}
            )
        )
        self.assertIn("lines 1-1 of 1", out)
        self.assertLessEqual(len(out), MAX_SLICE_CHARS + 300)
        self.assertIn("longer than one read", out)
        self.assertIn("offset=", out)
        self.assertNotIn("start_line=2", out)


# ---------------------------------------------------------------------------
# 3. Offload exclusions: prefix-exempt is not size-exempt
# ---------------------------------------------------------------------------


class OffloadExclusions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeArtifactStore()
        self.policy = ToolResultOffloadPolicy(
            self.store, ContextSettings(offload_threshold_chars=1_000)
        )

    async def _run(self, name: str, body: str) -> ToolMessage:
        msg = ToolMessage(content=body, tool_call_id="c1", name=name)
        with mock.patch(
            "yuyutsava.context.offload_policy.thread_id_from_runtime",
            return_value=THREAD,
        ):
            return await self.policy.after_tool(ToolCall(id="c1", name=name, args={}), msg)

    async def test_oversized_subagent_response_is_stored_not_suppressed(self):
        body = "s" * 200_000
        out = await self._run("task", body)

        digest = json.loads(out.content)
        self.assertTrue(digest["offloaded"])
        self.assertEqual(self.store.bodies[digest["artifact_id"]], body)
        # The old behaviour: exempt at every size, so this reached the guard.
        self.assertIsNot(guard_tool_result(body, "task"), body)
        self.assertIs(guard_tool_result(out.content, "task"), out.content)

    async def test_small_subagent_response_stays_inline(self):
        out = await self._run("task", "done: 3 files changed")
        self.assertEqual(out.content, "done: 3 files changed")
        self.assertEqual(self.store.puts, [])

    async def test_ctx_readers_are_never_offloaded(self):
        # Offloading a read-back would replace the content the agent just
        # asked for with a pointer to itself.
        for name in (
            "ctx_fetch_artifact",
            "ctx_grep_artifact",
            "ctx_history",
            "ctx_history_message",
            "ctx_history_grep",
        ):
            with self.subTest(tool=name):
                body = "r" * 50_000
                out = await self._run(name, body)
                self.assertEqual(out.content, body)
        self.assertEqual(self.store.puts, [])

    async def test_ws_prefix_still_offloads_when_small(self):
        out = await self._run("ws_tavily_search", "tiny result")
        self.assertTrue(json.loads(out.content)["offloaded"])


# ---------------------------------------------------------------------------
# 4. The last-resort guard offers real recovery
# ---------------------------------------------------------------------------


class GuardRecovery(unittest.TestCase):
    def test_generic_notice_names_a_way_forward(self):
        payload = json.dumps({"status": "ok", "result": {"blob": "z" * 200_000}})
        out = json.loads(guard_tool_result(payload, "some_tool"))

        notice = out["result"]
        self.assertTrue(notice["suppressed"])
        self.assertTrue(notice["recovery"], "an empty recovery list is a dead end")
        actions = {h["action"] for h in notice["recovery"]}
        self.assertIn("read_offloaded_copy", actions)
        blob = json.dumps(notice["recovery"])
        self.assertIn("ctx_grep_artifact", blob)


# ---------------------------------------------------------------------------
# 5. ctx_history: compacted turns stay readable
# ---------------------------------------------------------------------------


def sample_rows() -> list[Rec]:
    return [
        Rec(1, "human", {"content": "open youtube in my work profile"}),
        Rec(
            2,
            "ai",
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "t1",
                        "name": "tr_write_file",
                        "args": {
                            "path": "/ws/.yuyutsava/sandbox/find_profile.py",
                            "content": "import json\n" + "# body\n" * 400,
                        },
                    }
                ],
            },
        ),
        Rec(3, "tool", {"name": "tr_run_python", "content": "Profile 1 -> abhinav@example.com"}),
        Rec(5, "ai", {"content": "Opened YouTube in Profile 1."}),  # gap: seq 4 missing
    ]


class HistoryReadback(unittest.TestCase):
    def setUp(self):
        self.tools = tools_for(FakeArtifactStore(), FakeTranscriptStore(sample_rows()))

    def test_history_lists_every_turn_with_seqs(self):
        out = with_thread(lambda: run(self.tools["ctx_history"].ainvoke({})))
        self.assertIn("#1 human open youtube in my work profile", out)
        self.assertIn("#2 ai → tr_write_file(", out)
        self.assertIn("#3 tool ← tr_run_python: Profile 1", out)
        self.assertIn("#5 ai Opened YouTube in Profile 1.", out)

    def test_listing_entries_are_clipped_but_the_message_is_not(self):
        out = with_thread(lambda: run(self.tools["ctx_history"].ainvoke({})))
        self.assertNotIn("# body\n# body", out)  # the 2.8k-char arg is clipped

        full = with_thread(
            lambda: run(self.tools["ctx_history_message"].ainvoke({"seq": 2, "length": 50_000}))
        )
        self.assertIn("find_profile.py", full)
        self.assertIn("# body", full)
        self.assertIn("[history #2 ai chars 0-", full)

    def test_message_read_pages_and_never_exceeds_the_ceiling(self):
        rows = [Rec(1, "ai", {"content": "q" * 120_000})]
        tools = tools_for(FakeArtifactStore(), FakeTranscriptStore(rows))
        out = with_thread(
            lambda: run(tools["ctx_history_message"].ainvoke({"seq": 1, "length": 10_000_000}))
        )
        self.assertLessEqual(len(out), MAX_SLICE_CHARS + 200)
        self.assertIn("[more: call ctx_history_message(1, offset=40000)]", out)
        self.assertIs(guard_tool_result(out, "ctx_history_message"), out)

    def test_grep_finds_a_detail_a_summary_would_have_dropped(self):
        out = with_thread(
            lambda: run(
                self.tools["ctx_history_grep"].ainvoke({"pattern": r"abhinav@example\.com"})
            )
        )
        self.assertIn("#3 tool:", out)
        self.assertIn("abhinav@example.com", out)
        self.assertIn("ctx_history_message(seq)", out)

    def test_paging_respects_seq_gaps(self):
        out = with_thread(
            lambda: run(self.tools["ctx_history"].ainvoke({"after_seq": 3, "limit": 10}))
        )
        self.assertIn("#5 ai", out)
        self.assertNotIn("#3 tool", out)

    def test_bad_seq_is_an_error_not_wrong_content(self):
        # seq 4 does not exist; after_seq=3 would otherwise hand back seq 5.
        out = with_thread(lambda: run(self.tools["ctx_history_message"].ainvoke({"seq": 4})))
        self.assertIn("no message #4", out)
        self.assertNotIn("Opened YouTube", out)

    def test_no_live_thread_is_reported_not_crashed(self):
        with mock.patch(
            "yuyutsava.context.tools.thread_id_from_runtime", return_value="unknown"
        ):
            out = run(self.tools["ctx_history"].ainvoke({}))
        self.assertIn("no active thread", out)

    def test_store_failure_degrades_instead_of_failing_the_turn(self):
        class Broken:
            async def list_messages(self, *a, **k):
                raise RuntimeError("db down")

        tools = tools_for(FakeArtifactStore(), Broken())
        out = with_thread(lambda: run(tools["ctx_history"].ainvoke({})))
        self.assertIn("no recorded messages", out)


# ---------------------------------------------------------------------------
# 6. Compaction: arg stubbing is deduplication, not truncation
# ---------------------------------------------------------------------------


class ArgStubbing(unittest.TestCase):
    def _mw(self, *, history_readback=True):
        from yuyutsava.context.compaction import YuyutsavaCompactionMiddleware

        with mock.patch.object(
            YuyutsavaCompactionMiddleware, "__init__", lambda self, **kw: None
        ):
            mw = YuyutsavaCompactionMiddleware()
        mw._role = "cli"
        mw._history_readback = history_readback
        return mw

    @staticmethod
    def _call(name, args, cid="c1"):
        return {"id": cid, "name": name, "args": args}

    def _tail(self, first_msg):
        # 7 messages so the first is past the keep-recent window of 6.
        return [first_msg] + [HumanMessage(content=f"m{i}", id=f"h{i}") for i in range(6)]

    def test_bulky_write_is_replaced_by_a_pointer_to_the_file(self):
        body = "print('x')\n" * 400
        msg = AIMessage(
            content="",
            id="ai-1",
            tool_calls=[self._call("tr_write_file", {"path": "/ws/out.py", "content": body})],
        )
        out = self._mw()._stub_bulky_args(self._tail(msg))

        args = out[0].tool_calls[0]["args"]
        self.assertNotIn("print('x')", args["content"])
        self.assertIn("stubbed", args["content"])
        self.assertIn('tr_read_file("/ws/out.py")', args["content"])
        self.assertLess(len(args["content"]), len(body))
        # Identity is preserved: same message id, same call id and name, same
        # arg keys — what the provider serializes and what thought-signature
        # lookups key on.
        self.assertEqual(out[0].id, "ai-1")
        self.assertEqual(out[0].tool_calls[0]["id"], "c1")
        self.assertEqual(out[0].tool_calls[0]["name"], "tr_write_file")
        self.assertEqual(set(args), {"path", "content"})
        self.assertEqual(args["path"], "/ws/out.py")

    def test_artifact_content_points_at_the_verbatim_history(self):
        msg = AIMessage(
            content="",
            id="ai-2",
            tool_calls=[self._call("artifact_create", {"kind": "html", "content": "<p>" * 500})],
        )
        out = self._mw()._stub_bulky_args(self._tail(msg))
        self.assertIn("ctx_history_grep", out[0].tool_calls[0]["args"]["content"])

    def test_no_readback_and_no_locator_means_no_stub(self):
        # Nothing to point the model at → leave the argument alone rather than
        # replace it with an unresolvable stub.
        msg = AIMessage(
            content="",
            id="ai-3",
            tool_calls=[self._call("artifact_create", {"kind": "html", "content": "<p>" * 500})],
        )
        out = self._mw(history_readback=False)._stub_bulky_args(self._tail(msg))
        self.assertEqual(out[0].tool_calls[0]["args"]["content"], "<p>" * 500)

    def test_write_without_a_path_is_left_alone(self):
        msg = AIMessage(
            content="", id="ai-4",
            tool_calls=[self._call("tr_write_file", {"content": "z" * 4_000})],
        )
        out = self._mw()._stub_bulky_args(self._tail(msg))
        self.assertEqual(out[0].tool_calls[0]["args"]["content"], "z" * 4_000)

    def test_recent_messages_and_small_args_are_untouched(self):
        big = AIMessage(
            content="", id="recent",
            tool_calls=[self._call("tr_write_file", {"path": "/a", "content": "q" * 5_000})],
        )
        small = AIMessage(
            content="", id="small",
            tool_calls=[self._call("tr_write_file", {"path": "/b", "content": "short"})],
        )
        # `big` sits inside the keep-recent window here.
        out = self._mw()._stub_bulky_args([small] + self._tail(big)[1:] + [big])
        self.assertEqual(out[-1].tool_calls[0]["args"]["content"], "q" * 5_000)
        self.assertEqual(out[0].tool_calls[0]["args"]["content"], "short")

    def test_other_tools_are_never_stubbed(self):
        msg = AIMessage(
            content="", id="ai-5",
            tool_calls=[self._call("tr_execute_in_sandbox", {"command": "c" * 4_000})],
        )
        out = self._mw()._stub_bulky_args(self._tail(msg))
        self.assertEqual(out[0].tool_calls[0]["args"]["command"], "c" * 4_000)

    def test_readback_footer_only_promised_when_the_tools_exist(self):
        self.assertIn("ctx_history_grep", self._mw()._with_readback("S"))
        self.assertEqual(self._mw(history_readback=False)._with_readback("S"), "S")


# ---------------------------------------------------------------------------
# 7. The summary prompt tells the model the turns survive
# ---------------------------------------------------------------------------


class SummaryPrompt(unittest.TestCase):
    def test_placeholder_is_resolved_both_ways_and_messages_survives(self):
        from yuyutsava.context.compaction import (
            _READBACK_NOTE,
            YUYUTSAVA_SUMMARY_PROMPT,
        )

        self.assertIn("{readback_note}", YUYUTSAVA_SUMMARY_PROMPT)
        on = YUYUTSAVA_SUMMARY_PROMPT.replace("{readback_note}", _READBACK_NOTE)
        off = YUYUTSAVA_SUMMARY_PROMPT.replace("{readback_note}", "")
        for rendered in (on, off):
            # langchain formats {messages} itself — it must not be consumed.
            self.assertIn("{messages}", rendered)
            self.assertNotIn("{readback_note}", rendered)
        self.assertIn("ctx_history", on)
        self.assertNotIn("ctx_history", off)


if __name__ == "__main__":
    unittest.main(verbosity=2)
