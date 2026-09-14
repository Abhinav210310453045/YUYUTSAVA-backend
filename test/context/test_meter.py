"""Context meter: the number a user sees has to be defensible.

An instrument nobody trusts is worse than no instrument, so these pin the
properties that make the reading trustworthy rather than merely present:

* segment chars **sum to the prompt** — a row that silently swallows or
  duplicates bytes would make "free space" a lie;
* every prompt block is attributed by a marker **imported from the module that
  owns it**, so a reworded prompt cannot quietly re-attribute memory to system;
* estimates are calibrated against the provider's reported tokens and the
  correction is clamped, so one strange response cannot make the panel swing;
* measured and estimated stay distinguishable (``calibrated``, ``priced``);
* the meter cannot fail a turn — not when a subscriber raises, not when the
  registry is gone, not when the provider reports no usage at all.

Run:  .venv/bin/python test/context/test_meter.py
"""

from __future__ import annotations

import asyncio
import unittest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import yuyutsava.core.config  # noqa: F401 — import first (core/__init__ cycle)
from yuyutsava.context.config import ContextSettings
from yuyutsava.context.injector import MEMORY_BLOCK_PREFIX
from yuyutsava.context.meter import (
    SEGMENT_LABELS,
    ContextMeterPolicy,
    ContextSnapshot,
    MeterBus,
    bus,
    counters,
    note_compaction,
    note_offload,
    note_tool_registry,
    reset_sizing_caches,
    reset_thread_state,
    split_system_chars,
    tool_schema_chars,
)
from yuyutsava.memory.agent_memory import INDEX_BLOCK_HEADER
from yuyutsava.policy.types import ModelCall, Turn, Usage
from yuyutsava.skills.injector import SKILLS_BLOCK_PREFIX


def run(coro):
    return asyncio.run(coro)


#: The framework hands the prompt over as ``request.system_message``, which the
#: adapter surfaces as ``ModelCall.system_texts`` — NOT as a message in state
#: (``ModelRequest.messages`` is documented "excluding system message"). A call
#: built without it is not a realistic call, which is how "system prompt ≈0"
#: went unnoticed.
SYSTEM_PROMPT = "YOU ARE YUYUTSAVA.\n" + ("prompt body. " * 400)


def model_call(**kw) -> ModelCall:
    kw.setdefault("system_texts", (SYSTEM_PROMPT,))
    kw.setdefault("tool_names", ())
    return ModelCall(**kw)


class _FakeTool:
    def __init__(self, name: str, schema_len: int) -> None:
        self.name = name
        self._schema_len = schema_len


class _FakeRegistry:
    """Duck-types the two ToolRegistry methods the meter calls."""

    def __init__(self, tools: list[_FakeTool], catalog: str = "cat") -> None:
        self._tools = tools
        self._catalog = catalog

    def all_tools(self) -> list[_FakeTool]:
        return list(self._tools)

    def schema_block(self, tools: list[_FakeTool]) -> str:
        return "x" * sum(t._schema_len for t in tools)

    def catalog_block(self) -> str:
        return self._catalog


class SnapshotArithmetic(unittest.TestCase):
    def test_used_is_the_sum_of_the_segments(self):
        snap = ContextSnapshot(
            max_input_tokens=1_000,
            system_tokens=100, tools_tokens=200, memory_tokens=30,
            skills_tokens=20, messages_tokens=50,
        )
        self.assertEqual(snap.used_tokens, 400)
        self.assertEqual(snap.free_tokens, 600)
        self.assertAlmostEqual(snap.used_fraction, 0.4)

    def test_segments_report_every_row_including_zeros(self):
        # "skills 0" is the finding that indexed skills went unused; a row that
        # disappears when empty cannot report it.
        rows = ContextSnapshot().segments()
        self.assertEqual([r[0] for r in rows], [k for k, _ in SEGMENT_LABELS])
        self.assertEqual(len(rows), 5)

    def test_an_overfull_window_clamps_to_one_not_past_it(self):
        snap = ContextSnapshot(max_input_tokens=100, messages_tokens=500)
        self.assertEqual(snap.used_fraction, 1.0)
        self.assertEqual(snap.free_tokens, 0)

    def test_no_window_reports_zero_rather_than_dividing_by_it(self):
        self.assertEqual(ContextSnapshot(messages_tokens=10).used_fraction, 0.0)

    def test_cache_hit_fraction_needs_an_input_count(self):
        self.assertEqual(ContextSnapshot(cache_read_tokens=5).cache_hit_fraction, 0.0)
        snap = ContextSnapshot(input_tokens=100, cache_read_tokens=93)
        self.assertAlmostEqual(snap.cache_hit_fraction, 0.93)

    def test_wire_shape_carries_ordered_segments_and_both_groups(self):
        d = ContextSnapshot(thread_id="t1", messages_tokens=10).as_dict()
        self.assertEqual([s["key"] for s in d["segments"]],
                         [k for k, _ in SEGMENT_LABELS])
        self.assertIn("input_tokens", d["call"])
        self.assertIn("calls", d["session"])
        self.assertEqual(d["thread_id"], "t1")


class SegmentAttribution(unittest.TestCase):
    def test_chars_sum_to_the_whole_prompt(self):
        text = (
            "BASE PROMPT\n\n"
            + INDEX_BLOCK_HEADER + "- a: one\n- b: two\n"
            + MEMORY_BLOCK_PREFIX + "\n  - [fact] x\n"
            + SKILLS_BLOCK_PREFIX + "\n  - s: y"
        )
        parts = split_system_chars(text)
        self.assertEqual(sum(parts.values()), len(text))
        self.assertGreater(parts["memory"], 0)
        self.assertGreater(parts["skills"], 0)
        self.assertEqual(parts["system"], text.find(INDEX_BLOCK_HEADER.strip()))

    def test_a_prompt_with_no_known_block_is_all_system(self):
        parts = split_system_chars("just a prompt")
        self.assertEqual(parts["system"], len("just a prompt"))
        self.assertEqual(parts["memory"], 0)
        self.assertEqual(parts["skills"], 0)

    def test_empty_prompt_is_all_zeros(self):
        self.assertEqual(split_system_chars(""), {"system": 0, "memory": 0, "skills": 0})

    def test_markers_come_from_their_owning_modules(self):
        # The point of the promoted constants: if a prompt is reworded, this
        # attribution follows it instead of silently going to `system`.
        text = INDEX_BLOCK_HEADER + "body"
        self.assertEqual(split_system_chars(text)["system"], 0)
        self.assertEqual(split_system_chars(text)["memory"], len(text))


class ToolSchemaSizing(unittest.TestCase):
    def setUp(self):
        reset_sizing_caches()

    def tearDown(self):
        reset_sizing_caches()

    def test_only_the_bound_tools_are_counted(self):
        # Lazy discovery binds a handful out of dozens, and that gap is what
        # the "tool schemas" row exists to show.
        # The strong reference matters: the meter holds registries weakly, so a
        # local built inline would be collected before the first lookup.
        self.registry = _FakeRegistry([
            _FakeTool("tool_search", 100),
            _FakeTool("tr_write_file", 900),
            _FakeTool("ws_tavily_search", 700),
        ])
        note_tool_registry("cli", self.registry)
        self.assertEqual(tool_schema_chars("cli", ("tool_search",)), 100)
        self.assertEqual(
            tool_schema_chars("cli", ("tool_search", "tr_write_file")), 1000)

    def test_an_unknown_name_costs_nothing_rather_than_raising(self):
        self.registry = _FakeRegistry([])
        note_tool_registry("cli", self.registry)
        self.assertEqual(tool_schema_chars("cli", ("nope",)), 0)

    def test_a_role_with_no_registry_falls_back_to_any_live_one(self):
        # Builders name registries by agent, policies carry a role; the two do
        # not always coincide and a 0 row would look like a finding.
        self.registry = _FakeRegistry([_FakeTool("t", 50)])
        note_tool_registry("cli", self.registry)
        self.assertEqual(tool_schema_chars("orchestrator", ("t",)), 50)

    def test_no_registry_at_all_is_zero_not_an_error(self):
        self.assertEqual(tool_schema_chars("cli", ("t",)), 0)

    def test_a_collected_registry_degrades_to_zero_rather_than_raising(self):
        # In production the registry is kept alive by the tool_search tool
        # bound to the graph; if that ever stops being true the row reads 0,
        # which is why the weak reference is documented where it is taken.
        note_tool_registry("cli", _FakeRegistry([_FakeTool("t", 50)]))
        import gc

        gc.collect()
        self.assertEqual(tool_schema_chars("cli", ("t",)), 0)


class BusFanout(unittest.TestCase):
    def test_a_subscriber_only_hears_its_own_thread(self):
        b = MeterBus()
        seen: list[str] = []
        b.subscribe("t1", lambda s: seen.append(s.thread_id))
        b.publish(ContextSnapshot(thread_id="t1"))
        b.publish(ContextSnapshot(thread_id="t2"))
        self.assertEqual(seen, ["t1"])

    def test_an_empty_thread_id_subscribes_to_everything(self):
        b = MeterBus()
        seen: list[str] = []
        b.subscribe("", lambda s: seen.append(s.thread_id))
        b.publish(ContextSnapshot(thread_id="t1"))
        b.publish(ContextSnapshot(thread_id="t2"))
        self.assertEqual(seen, ["t1", "t2"])

    def test_unsubscribe_stops_delivery(self):
        b = MeterBus()
        seen: list[str] = []
        token = b.subscribe("", lambda s: seen.append(s.thread_id))
        b.unsubscribe(token)
        b.publish(ContextSnapshot(thread_id="t1"))
        self.assertEqual(seen, [])

    def test_a_raising_subscriber_cannot_break_the_publish(self):
        # Callbacks run inside the agent's model step. A broken display must
        # not be able to fail a turn.
        b = MeterBus()
        seen: list[str] = []

        def boom(_s):
            raise RuntimeError("display exploded")

        b.subscribe("", boom)
        b.subscribe("", lambda s: seen.append(s.thread_id))
        b.publish(ContextSnapshot(thread_id="t1"))
        self.assertEqual(seen, ["t1"])

    def test_latest_serves_a_client_that_just_connected(self):
        b = MeterBus()
        self.assertIsNone(b.latest("t1"))
        b.publish(ContextSnapshot(thread_id="t1", messages_tokens=5))
        self.assertEqual(b.latest("t1").messages_tokens, 5)

    def test_thread_state_is_bounded(self):
        b = MeterBus()
        for i in range(200):
            b.publish(ContextSnapshot(thread_id=f"t{i}"))
        self.assertLessEqual(len(b._latest), 64)
        self.assertIsNotNone(b.latest("t199"))


class Counters(unittest.TestCase):
    def setUp(self):
        reset_sizing_caches()

    def tearDown(self):
        reset_sizing_caches()

    def test_compactions_and_offloads_count_per_thread(self):
        note_compaction("a")
        note_compaction("a")
        note_offload("a")
        note_offload("b")
        self.assertEqual(counters("a"), (2, 1))
        self.assertEqual(counters("b"), (0, 1))
        self.assertEqual(counters("unknown"), (0, 0))

    def test_a_missing_thread_id_is_not_counted_anywhere(self):
        note_compaction("")
        note_offload("")
        self.assertEqual(counters(""), (0, 0))


class PolicyPublishing(unittest.TestCase):
    def setUp(self):
        reset_sizing_caches()
        reset_thread_state()
        bus().clear()
        self.settings = ContextSettings(max_input_tokens=1_000_000)
        self.policy = ContextMeterPolicy(
            settings=self.settings, role="cli", model_name="gemini-3.5-flash"
        )
        # Held strongly for the test's lifetime — the meter keeps only a weakref.
        self.registry = _FakeRegistry([_FakeTool("tool_search", 400)], "cat")
        note_tool_registry("cli", self.registry)
        self.seen: list[ContextSnapshot] = []
        bus().subscribe("", self.seen.append)

    def tearDown(self):
        bus().clear()
        reset_thread_state()
        reset_sizing_caches()

    def _turn(self, *, usage=None) -> Turn:
        return Turn(
            messages=(
                SystemMessage(content="BASE PROMPT " + "p" * 400),
                HumanMessage(content="hello " + "h" * 200),
                AIMessage(content="hi"),
                ToolMessage(content='{"offloaded": true, "id": "art_1"}',
                            tool_call_id="c1", name="ws_tavily_search"),
            ),
            thread_id="t1",
            usage=usage,
        )

    def test_the_window_and_the_trigger_come_from_the_settings(self):
        run(self.policy.before_model(self._turn()))
        run(self.policy.revise_model_call(model_call(tool_names=("tool_search",))))
        snap = self.seen[-1]
        self.assertEqual(snap.max_input_tokens, 1_000_000)
        self.assertEqual(snap.compact_trigger_tokens,
                         self.settings.compact_trigger_tokens)

    def test_the_reviser_publishes_tool_schema_cost_and_the_count(self):
        run(self.policy.before_model(self._turn()))
        run(self.policy.revise_model_call(model_call(tool_names=("tool_search",))))
        snap = self.seen[-1]
        self.assertGreater(snap.tools_tokens, 0)
        self.assertEqual(snap.tool_count, 1)
        self.assertGreater(snap.system_tokens, 0)
        self.assertGreater(snap.messages_tokens, 0)

    def test_the_system_prompt_is_never_reported_as_zero(self):
        """The bug the panel showed on every call: "system prompt ≈0".

        It came from segmenting ``turn.messages``, which excludes the system
        message — the prompt travels as ``request.system_message``. A prompt
        was sent, so a prompt must be counted.
        """
        run(self.policy.before_model(self._turn()))
        run(self.policy.revise_model_call(model_call()))
        snap = self.seen[-1]
        self.assertGreater(snap.system_tokens, 500, "the prompt vanished again")

    def test_a_system_message_in_state_is_not_counted_twice(self):
        # If a SystemMessage also appears in state it is the same bytes the
        # framework moved into system_message — counting both would inflate
        # the window by the size of the prompt.
        run(self.policy.before_model(Turn(
            messages=(SystemMessage(content=SYSTEM_PROMPT),), thread_id="t1")))
        run(self.policy.revise_model_call(model_call()))
        only_prompt = self.seen[-1]
        run(self.policy.before_model(Turn(messages=(), thread_id="t1")))
        run(self.policy.revise_model_call(model_call()))
        self.assertEqual(only_prompt.system_tokens, self.seen[-1].system_tokens)

    def test_the_agent_memory_block_inside_the_prompt_is_attributed(self):
        run(self.policy.before_model(self._turn()))
        run(self.policy.revise_model_call(model_call(
            system_texts=(SYSTEM_PROMPT + "\n\n" + INDEX_BLOCK_HEADER + "x" * 900,))))
        self.assertGreater(self.seen[-1].memory_tokens, 0)

    def test_offloaded_digests_are_counted_not_charged_as_prose(self):
        run(self.policy.before_model(self._turn()))
        run(self.policy.revise_model_call(model_call()))
        self.assertEqual(self.seen[-1].offloaded_digests, 1)

    def test_revise_model_call_records_no_edits(self):
        # Pure observability: the adapter must hand the request through
        # untouched, so `changed` has to stay False.
        call = model_call(tool_names=("tool_search",))
        run(self.policy.before_model(self._turn()))
        run(self.policy.revise_model_call(call))
        self.assertFalse(call.changed)

    def test_a_call_the_provider_did_not_report_publishes_nothing(self):
        before = len(self.seen)
        run(self.policy.after_model(self._turn(usage=None)))
        run(self.policy.after_model(self._turn(usage=Usage())))
        self.assertEqual(len(self.seen), before)

    def test_last_call_figures_are_the_provider_numbers_verbatim(self):
        run(self.policy.after_model(self._turn(usage=Usage(
            input_tokens=41_087, output_tokens=64,
            cache_read_tokens=38_000, cache_creation_tokens=12,
        ))))
        snap = self.seen[-1]
        self.assertEqual(snap.input_tokens, 41_087)
        self.assertEqual(snap.output_tokens, 64)
        self.assertEqual(snap.cache_read_tokens, 38_000)
        self.assertEqual(snap.cache_creation_tokens, 12)
        self.assertEqual(snap.call_no, 1)

    def test_session_totals_accumulate_across_calls(self):
        for _ in range(3):
            run(self.policy.after_model(self._turn(usage=Usage(
                input_tokens=100, output_tokens=10, cache_read_tokens=50))))
        snap = self.seen[-1]
        self.assertEqual(snap.calls, 3)
        self.assertEqual(snap.session_input_tokens, 300)
        self.assertEqual(snap.session_output_tokens, 30)
        self.assertEqual(snap.session_cache_read_tokens, 150)

    def test_totals_are_kept_per_thread(self):
        run(self.policy.after_model(Turn(thread_id="a", usage=Usage(input_tokens=100))))
        run(self.policy.after_model(Turn(thread_id="b", usage=Usage(input_tokens=7))))
        self.assertEqual(bus().latest("a").session_input_tokens, 100)
        self.assertEqual(bus().latest("b").session_input_tokens, 7)

    def test_estimates_are_marked_uncalibrated_until_a_call_completes(self):
        run(self.policy.before_model(self._turn()))
        run(self.policy.revise_model_call(model_call()))
        self.assertFalse(self.seen[-1].calibrated)
        run(self.policy.after_model(self._turn(usage=Usage(input_tokens=500))))
        self.assertTrue(self.seen[-1].calibrated)

    def test_calibration_pulls_the_estimate_toward_the_reported_number(self):
        run(self.policy.before_model(self._turn()))
        run(self.policy.revise_model_call(model_call()))
        estimated = self.seen[-1].used_tokens
        # Provider says the same prompt was worth ~1.5x our estimate.
        run(self.policy.after_model(self._turn(
            usage=Usage(input_tokens=int(estimated * 1.5)))))
        run(self.policy.before_model(self._turn()))
        run(self.policy.revise_model_call(model_call()))
        self.assertGreater(self.seen[-1].used_tokens, estimated)

    def test_a_wild_ratio_is_clamped_rather_than_trusted(self):
        run(self.policy.before_model(self._turn()))
        run(self.policy.revise_model_call(model_call()))
        estimated = self.seen[-1].used_tokens
        run(self.policy.after_model(self._turn(usage=Usage(input_tokens=10_000_000))))
        run(self.policy.before_model(self._turn()))
        run(self.policy.revise_model_call(model_call()))
        self.assertLessEqual(self.seen[-1].used_tokens, estimated * 2 + 1)

    def test_an_unpriced_model_says_so_instead_of_showing_a_confident_zero(self):
        run(self.policy.after_model(self._turn(usage=Usage(input_tokens=100))))
        self.assertFalse(self.seen[-1].priced)
        self.assertEqual(self.seen[-1].est_cost_usd, 0.0)

    def test_a_priced_model_reports_a_cost(self):
        policy = ContextMeterPolicy(
            settings=self.settings, role="cli", model_name="claude-sonnet-4-5"
        )
        run(policy.after_model(Turn(
            thread_id="p", usage=Usage(input_tokens=1_000_000, output_tokens=0))))
        snap = bus().latest("p")
        self.assertTrue(snap.priced)
        self.assertGreater(snap.est_cost_usd, 0.0)

    def test_compactions_and_offloads_reach_the_snapshot(self):
        note_compaction("t1")
        note_offload("t1")
        note_offload("t1")
        run(self.policy.before_model(self._turn()))
        run(self.policy.revise_model_call(model_call()))
        self.assertEqual(self.seen[-1].compactions, 1)
        self.assertEqual(self.seen[-1].offloads, 2)

    def test_a_reviser_without_a_preceding_measurement_publishes_nothing(self):
        before = len(self.seen)
        run(self.policy.revise_model_call(model_call(tool_names=("tool_search",))))
        self.assertEqual(len(self.seen), before)

    def test_hooks_return_none_so_nothing_is_injected(self):
        self.assertIsNone(run(self.policy.before_model(self._turn())))
        self.assertIsNone(run(self.policy.after_model(
            self._turn(usage=Usage(input_tokens=1)))))


class SubagentsReportSpendButNotTheWindow(unittest.TestCase):
    """A subagent runs its own message list inside the master's turn.

    Publishing its segments would make the panel jump to a different
    conversation's shape mid-delegation; ignoring its tokens would under-report
    money the conversation really spent. So: spend yes, window no.
    """

    def setUp(self):
        reset_sizing_caches()
        reset_thread_state()
        bus().clear()
        settings = ContextSettings(max_input_tokens=500_000)
        self.master = ContextMeterPolicy(
            settings=settings, role="cli", model_name="claude-sonnet-4-5")
        self.sub = ContextMeterPolicy(
            settings=settings, role="general-purpose",
            model_name="claude-haiku-4-5", window=False)

    def tearDown(self):
        bus().clear()
        reset_thread_state()
        reset_sizing_caches()

    def _master_measure(self):
        run(self.master.before_model(Turn(
            messages=(SystemMessage(content="base " + "b" * 2_000),
                      HumanMessage(content="q" * 800)),
            thread_id="t1")))
        run(self.master.revise_model_call(model_call()))

    def test_a_subagent_does_not_redraw_the_segments(self):
        self._master_measure()
        master_rows = bus().latest("t1").segments()
        run(self.sub.before_model(Turn(
            messages=(SystemMessage(content="tiny"),), thread_id="t1")))
        run(self.sub.revise_model_call(model_call()))
        self.assertEqual(bus().latest("t1").segments(), master_rows)

    def test_a_subagents_tokens_add_to_the_conversation_total(self):
        self._master_measure()
        run(self.master.after_model(Turn(
            thread_id="t1", usage=Usage(input_tokens=1_000, output_tokens=100))))
        run(self.sub.after_model(Turn(
            thread_id="t1", usage=Usage(input_tokens=400, output_tokens=40))))
        snap = bus().latest("t1")
        self.assertEqual(snap.calls, 2)
        self.assertEqual(snap.session_input_tokens, 1_400)
        self.assertEqual(snap.session_output_tokens, 140)

    def test_a_subagent_call_keeps_the_window_rows_it_did_not_measure(self):
        self._master_measure()
        before = bus().latest("t1").used_tokens
        run(self.sub.after_model(Turn(
            thread_id="t1", usage=Usage(input_tokens=400))))
        self.assertEqual(bus().latest("t1").used_tokens, before)

    def test_a_subagent_does_not_relabel_the_panel(self):
        self._master_measure()
        run(self.sub.after_model(Turn(
            thread_id="t1", usage=Usage(input_tokens=400))))
        snap = bus().latest("t1")
        self.assertEqual(snap.role, "cli")
        self.assertEqual(snap.model, "claude-sonnet-4-5")

    def test_a_subagent_never_calibrates_the_masters_estimate(self):
        # It has no pending estimate of its own, and its prompt is not the one
        # the master measured — dividing by that would corrupt the scale.
        self._master_measure()
        run(self.sub.after_model(Turn(
            thread_id="t1", usage=Usage(input_tokens=9_999_999))))
        self.assertFalse(bus().latest("t1").calibrated)


class InjectedBlocksAreCounted(unittest.TestCase):
    """Skills/memory blocks are appended at model-call time.

    Every ``before_model`` hook has already run by then, so counting only the
    message list under-reports the prompt by whatever was recalled.
    """

    def setUp(self):
        reset_sizing_caches()
        reset_thread_state()
        bus().clear()
        self.seen: list[ContextSnapshot] = []
        bus().subscribe("", self.seen.append)

    def tearDown(self):
        bus().clear()
        reset_thread_state()
        reset_sizing_caches()

    def test_a_recalled_skills_block_lands_in_the_skills_row(self):
        from yuyutsava.retrieval import injector as ri

        ri._last_blocks.clear()
        ri._last_blocks[SKILLS_BLOCK_PREFIX] = 4_000
        policy = ContextMeterPolicy(
            settings=ContextSettings(max_input_tokens=1_000), role="cli")
        run(policy.before_model(Turn(
            messages=(SystemMessage(content="base"),), thread_id="t")))
        run(policy.revise_model_call(model_call()))
        self.assertGreater(self.seen[-1].skills_tokens, 0)
        ri._last_blocks.clear()

    def test_nothing_recalled_reports_zero_not_last_turns_number(self):
        from yuyutsava.retrieval import injector as ri

        ri._last_blocks.clear()
        ri._last_blocks[SKILLS_BLOCK_PREFIX] = 0
        policy = ContextMeterPolicy(
            settings=ContextSettings(max_input_tokens=1_000), role="cli")
        run(policy.before_model(Turn(
            messages=(SystemMessage(content="base"),), thread_id="t")))
        run(policy.revise_model_call(model_call()))
        self.assertEqual(self.seen[-1].skills_tokens, 0)
        ri._last_blocks.clear()


# ----------------------------------------------------------------------
# 14 Sep: "same chat first shows 26k token and after next call shows 23.3k"
# ----------------------------------------------------------------------


class TheHeadlineFollowsTheProvider(unittest.TestCase):
    """The window total may not move for reasons unrelated to the window.

    Measured from two screenshots of one conversation. On call #1 the segments
    summed to 26.87k (6.3k + 19.9k + 668 + 0 + 5) while the provider reported
    **13.4k** for that very prompt — a 0.499 ratio, pinned at the old 0.5
    calibration floor. By call #8 every row had been multiplied by that factor
    and the total had *fallen* to 23.57k, while the provider was now reporting
    **27.6k**. The panel printed both, four rows apart, and the headline had
    moved the wrong way for a reason that had nothing to do with the
    conversation.

    So the headline is anchored on the provider's number and only the growth
    since is estimated, which makes these invariants testable.
    """

    def setUp(self):
        reset_sizing_caches()
        reset_thread_state()
        bus().clear()
        self.settings = ContextSettings(max_input_tokens=1_000_000)
        self.policy = ContextMeterPolicy(
            settings=self.settings, role="cli", model_name="gemini-3.5-flash"
        )
        # A fat schema block, because the tool schemas row is what the
        # character estimate overcounts: ~20k chars of JSON reach Gemini as
        # FunctionDeclaration protos.
        self.registry = _FakeRegistry([_FakeTool("tool_search", 20_000)], "cat")
        note_tool_registry("cli", self.registry)
        self.seen: list[ContextSnapshot] = []
        bus().subscribe("", self.seen.append)

    def tearDown(self):
        bus().clear()
        reset_thread_state()
        reset_sizing_caches()

    def _turn(self, n_messages: int, *, usage=None) -> Turn:
        msgs: list = [HumanMessage(content="ask " + "q" * 300)]
        for i in range(n_messages):
            msgs.append(AIMessage(content="reply " + "r" * 300))
            msgs.append(HumanMessage(content=f"follow {i} " + "f" * 300))
        return Turn(messages=tuple(msgs), thread_id="t1", usage=usage)

    def _call(self, n_messages: int, reported_in: int, reported_out: int = 40):
        """One full model call. Returns ``(pre_call, post_call)`` snapshots."""
        run(self.policy.before_model(self._turn(n_messages)))
        run(self.policy.revise_model_call(model_call(tool_names=("tool_search",))))
        pre = self.seen[-1]
        run(self.policy.after_model(Turn(
            thread_id="t1",
            usage=Usage(input_tokens=reported_in, output_tokens=reported_out),
        )))
        return pre, self.seen[-1]

    #: The eight calls of the real session, with the provider's numbers.
    REPORTED = (13_400, 16_000, 18_500, 21_000, 23_000, 25_000, 26_500, 27_600)

    def _sequence(self):
        """Drive the real session's shape. Returns pre/post totals per call."""
        pre_totals: list[int] = []
        post_totals: list[int] = []
        for i, reported in enumerate(self.REPORTED):
            pre, post = self._call(1 + i * 3, reported)
            pre_totals.append(pre.used_tokens)
            post_totals.append(post.used_tokens)
        return pre_totals, post_totals

    def test_the_measured_total_only_ever_grows_with_the_conversation(self):
        """The regression, as an invariant.

        Every post-call total is the provider's own arithmetic, so this is the
        sequence that may not wobble at all. The old code produced 26.9k then
        23.5k here while the truth went 13.4k -> 27.6k.
        """
        _pre, post = self._sequence()
        for earlier, later in zip(post, post[1:]):
            self.assertLessEqual(
                earlier, later,
                f"a measured window shrank from {earlier} to {later} with "
                f"nothing removed — sequence {post}",
            )

    def test_an_estimate_is_never_below_the_last_measurement(self):
        # Nothing was removed, so the window cannot be smaller than what the
        # provider just counted.
        pre, post = self._sequence()
        for measured, next_estimate in zip(post, pre[1:]):
            self.assertGreaterEqual(next_estimate, measured)

    def test_a_measurement_may_correct_an_estimate_but_only_slightly(self):
        """The honest limit, bounded.

        Growth between calls is still estimated, so a measurement can revise
        the headline down when the estimate ran high — that is a measurement
        doing its job. What may not happen is the *scale* being rewritten
        underneath the conversation: the old single factor moved the total by
        -12.6% while the real prompt was doubling. Anything beyond a few
        percent per call means the anchor has stopped holding.
        """
        pre, post = self._sequence()
        for estimate, measured in zip(pre, post):
            if measured >= estimate:
                continue
            drift = (estimate - measured) / max(1, estimate)
            self.assertLess(
                drift, 0.05,
                f"a measurement cut the headline by {drift:.1%} "
                f"({estimate} -> {measured}); the growth estimate is no "
                f"longer tracking the anchor",
            )

    def test_the_headline_is_the_providers_arithmetic_right_after_a_call(self):
        _pre, post = self._call(4, 13_400, reported_out=201)
        # Both terms are provider numbers, so nothing here is estimated.
        self.assertEqual(post.used_tokens, 13_400 + 201)
        self.assertTrue(post.window_measured)
        self.assertTrue(post.anchored)

    def test_the_rows_sum_to_the_headline_exactly(self):
        _pre, post = self._call(6, 27_600, reported_out=201)
        self.assertEqual(
            sum(tokens for _k, _l, tokens in post.segments()),
            post.used_tokens,
            "a panel whose parts do not add up to its total is the complaint",
        )

    def test_the_headline_no_longer_contradicts_the_last_call_row(self):
        # 23.5k printed four rows above "in 27.6k" was the visible symptom.
        _pre, post = self._call(6, 27_600, reported_out=0)
        self.assertEqual(post.used_tokens, post.input_tokens)

    def test_growth_between_calls_is_flagged_as_an_estimate(self):
        self._call(2, 13_400)
        run(self.policy.before_model(self._turn(9)))
        run(self.policy.revise_model_call(model_call(tool_names=("tool_search",))))
        pre = self.seen[-1]
        self.assertTrue(pre.anchored)
        self.assertFalse(
            pre.window_measured,
            "messages were appended since the measurement, so the total is "
            "part estimate and must say so",
        )
        self.assertGreater(pre.used_tokens, 13_400)

    def test_a_prefix_only_call_measures_the_prefix(self):
        # Call #1 carries ~no message tokens, so the provider's number IS the
        # prefix — a free exact reading, and the reason two factors are
        # separable at all.
        pre, _post = self._call(0, 13_400)
        uncorrected = pre.used_tokens
        run(self.policy.before_model(self._turn(0)))
        run(self.policy.revise_model_call(model_call(tool_names=("tool_search",))))
        corrected = self.seen[-1]
        self.assertNotEqual(uncorrected, corrected.used_tokens)
        self.assertAlmostEqual(corrected.used_tokens, 13_400, delta=200)
        self.assertTrue(corrected.calibrated)

    def test_a_second_conversation_on_the_same_model_starts_calibrated(self):
        self._call(0, 13_400)
        other = ContextMeterPolicy(
            settings=self.settings, role="cli", model_name="gemini-3.5-flash"
        )
        run(other.before_model(Turn(
            messages=(HumanMessage(content="hi"),), thread_id="t2")))
        run(other.revise_model_call(model_call(tool_names=("tool_search",))))
        fresh = bus().latest("t2")
        self.assertTrue(
            fresh.calibrated,
            "the factors describe the model, not the thread — a new chat "
            "should not read 2x high for one call",
        )
        self.assertAlmostEqual(fresh.used_tokens, 13_400, delta=1_500)

    def test_a_different_model_does_not_inherit_the_correction(self):
        self._call(0, 13_400)
        other = ContextMeterPolicy(
            settings=self.settings, role="cli", model_name="claude-sonnet-4-5"
        )
        run(other.before_model(Turn(
            messages=(HumanMessage(content="hi"),), thread_id="t3")))
        run(other.revise_model_call(model_call(tool_names=("tool_search",))))
        self.assertFalse(bus().latest("t3").calibrated)

    def test_an_implausible_reading_is_not_anchored_on(self):
        # A provider reporting cumulative tokens, or a mid-call compaction:
        # the two numbers are not describing the same prompt, so the spend is
        # kept and the window scale is not touched.
        pre, _post = self._call(3, 10_000_000)
        run(self.policy.before_model(self._turn(3)))
        run(self.policy.revise_model_call(model_call(tool_names=("tool_search",))))
        after = self.seen[-1]
        self.assertFalse(after.anchored)
        self.assertLess(after.used_tokens, pre.used_tokens * 2 + 1)
        # The money still counts.
        self.assertEqual(after.session_input_tokens, 10_000_000)

    def test_compaction_lets_the_total_fall_again(self):
        # The one legitimate reason to shrink: content actually left the
        # window. A stale high-water mark would hide it.
        self._call(12, 40_000)
        run(self.policy.before_model(self._turn(1)))
        run(self.policy.revise_model_call(model_call(tool_names=("tool_search",))))
        self.assertLess(self.seen[-1].used_tokens, 40_000)

    def test_the_wire_shape_carries_the_two_new_flags(self):
        _pre, post = self._call(2, 13_400)
        d = post.as_dict()
        self.assertIn("anchored", d)
        self.assertIn("window_measured", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
