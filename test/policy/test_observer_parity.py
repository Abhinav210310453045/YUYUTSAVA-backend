"""The four observer policies behave exactly as their middlewares did.

Phase 4 step 4.8, the last migration.

These four never revise a request and only one of them ever changes state, so the
things worth pinning are what they *record* and when:

* **transcript** — which messages reach the store, on which phases, and that a
  message is never written twice;
* **budget** — the running total, the exact cap boundary, and that the wrap-up
  directive fires **once**;
* **usage** — one row per model call, with the right tokens, model and cost, and
  *no* row when the provider reported nothing;
* **prompt inspector** — pure logging, gated on an env var.

The middlewares were deleted at cutover; ``_Recorded`` drives the policies
through the adapter, which is the production path, and the expectations are the
ones read off the middleware source before it went. Budget's wrap-up text is
compared verbatim: it is a prompt the model reads.

Run:  .venv/bin/python test/policy/test_observer_parity.py
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from yuyutsava.policy.adapter import LangChainPolicyAdapter

THREAD = "thr-observer"


def _ai(text: str, *, msg_id: str = "a1", input_tokens: int | None = None,
        output_tokens: int = 0, model: str = "") -> AIMessage:
    msg = AIMessage(content=text, id=msg_id)
    if input_tokens is not None:
        msg.usage_metadata = {  # type: ignore[attr-defined]
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
    if model:
        msg.response_metadata = {"model_name": model}  # type: ignore[attr-defined]
    return msg


async def _observe(policy: Any, phase: str, messages: list[Any],
                   thread_id: str = THREAD) -> dict | None:
    """Drive one phase through the adapter, the way the graph does."""
    adapter = LangChainPolicyAdapter([policy])
    with patch("yuyutsava.policy.adapter._current_thread_id", lambda: thread_id):
        return await getattr(adapter, f"a{phase}")({"messages": messages}, None)


class _Store:
    def __init__(self) -> None:
        self.written: list[tuple[str, list[Any]]] = []

    async def put_messages(self, thread_id: str, messages: list[Any]) -> None:
        self.written.append((thread_id, list(messages)))


class _FailingStore(_Store):
    async def put_messages(self, thread_id: str, messages: list[Any]) -> None:
        raise RuntimeError("postgres is down")


class Transcript(unittest.IsolatedAsyncioTestCase):
    def _policy(self, store: Any, index: Any = None):
        from yuyutsava.context.transcript_policy import TranscriptRecorderPolicy

        return TranscriptRecorderPolicy(store, index=index)

    async def test_it_records_on_all_three_phases(self) -> None:
        for phase in ("before_model", "after_model", "after_agent"):
            with self.subTest(phase=phase):
                store = _Store()
                await _observe(self._policy(store), phase, [HumanMessage("hi", id="h1")])
                self.assertEqual(len(store.written), 1, f"{phase} recorded nothing")

    async def test_a_message_is_written_once(self) -> None:
        store = _Store()
        policy = self._policy(store)
        messages = [HumanMessage("hi", id="h1")]
        await _observe(policy, "before_model", messages)
        await _observe(policy, "after_model", messages)
        self.assertEqual(
            len(store.written), 1,
            "the same message was persisted twice; the seen-set is not working",
        )

    async def test_only_new_messages_are_written(self) -> None:
        store = _Store()
        policy = self._policy(store)
        await _observe(policy, "before_model", [HumanMessage("hi", id="h1")])
        await _observe(policy, "after_model",
                       [HumanMessage("hi", id="h1"), _ai("there", msg_id="a1")])
        self.assertEqual([m.id for m in store.written[1][1]], ["a1"])

    async def test_messages_without_an_id_are_skipped(self) -> None:
        store = _Store()
        await _observe(self._policy(store), "before_model", [HumanMessage("hi")])
        self.assertEqual(store.written, [], "an id-less message cannot be deduped")

    async def test_nothing_is_written_without_a_thread(self) -> None:
        store = _Store()
        await _observe(self._policy(store), "before_model",
                       [HumanMessage("hi", id="h1")], thread_id="")
        self.assertEqual(store.written, [])

    async def test_a_store_failure_never_fails_the_turn(self) -> None:
        with self.assertLogs("yuyutsava.context.transcript", level="ERROR"):
            result = await _observe(
                self._policy(_FailingStore()), "before_model",
                [HumanMessage("hi", id="h1")])
        self.assertIsNone(result)

    async def test_a_failed_write_is_retried_next_phase(self) -> None:
        """The seen-set must not record what was never persisted."""
        store = _FailingStore()
        policy = self._policy(store)
        with self.assertLogs("yuyutsava.context.transcript", level="ERROR") as cm:
            await _observe(policy, "before_model", [HumanMessage("hi", id="h1")])
            await _observe(policy, "after_model", [HumanMessage("hi", id="h1")])
        self.assertEqual(len(cm.output), 2, "the message was given up on after one failure")

    async def test_a_broken_index_never_fails_the_turn(self) -> None:
        class _BadIndex:
            def index_messages(self, thread_id: str, messages: list[Any]) -> None:
                raise RuntimeError("embedder down")

        store = _Store()
        result = await _observe(
            self._policy(store, index=_BadIndex()), "before_model",
            [HumanMessage("hi", id="h1")])
        self.assertIsNone(result)
        self.assertEqual(len(store.written), 1, "the store write was rolled back")


class Budget(unittest.IsolatedAsyncioTestCase):
    def _policy(self, cap: int = 100):
        from yuyutsava.daemon.budget_policy import BudgetPolicy

        return BudgetPolicy(max_input_tokens=cap, role="cli")

    async def test_under_the_cap_is_silent(self) -> None:
        result = await _observe(self._policy(), "after_model", [_ai("x", input_tokens=50)])
        self.assertIsNone(result)

    async def test_the_cap_boundary_is_inclusive(self) -> None:
        """``>=`` — one token below is silent, exactly at the cap fires."""
        self.assertIsNone(
            await _observe(self._policy(), "after_model", [_ai("x", input_tokens=99)]))
        self.assertIsNotNone(
            await _observe(self._policy(), "after_model", [_ai("x", input_tokens=100)]))

    async def test_the_total_accumulates_across_calls(self) -> None:
        policy = self._policy()
        for _ in range(3):
            result = await _observe(policy, "after_model", [_ai("x", input_tokens=30)])
        self.assertIsNone(result, "90 tokens should not have tripped a cap of 100")
        result = await _observe(policy, "after_model", [_ai("x", input_tokens=30)])
        self.assertIsNotNone(result, "120 tokens did not trip a cap of 100")

    async def test_the_directive_text_is_unchanged(self) -> None:
        """The model reads this. Compared verbatim against the middleware's."""
        result = await _observe(self._policy(), "after_model", [_ai("x", input_tokens=250)])
        (message,) = result["messages"]
        self.assertIsInstance(message, SystemMessage)
        self.assertEqual(
            message.content,
            "Token budget for this task is exhausted "
            "(250 input tokens used, cap 100). "
            "Stop calling tools. Summarise what you have done so far "
            "and what remains in your final reply.",
        )

    async def test_it_fires_exactly_once(self) -> None:
        policy = self._policy()
        first = await _observe(policy, "after_model", [_ai("x", input_tokens=250)])
        second = await _observe(policy, "after_model", [_ai("x", input_tokens=250)])
        self.assertIsNotNone(first)
        self.assertIsNone(
            second, "the wrap-up directive repeats every turn once the cap is hit")

    async def test_reset_re_arms_it(self) -> None:
        policy = self._policy()
        await _observe(policy, "after_model", [_ai("x", input_tokens=250)])
        policy.reset()
        self.assertIsNone(
            await _observe(policy, "after_model", [_ai("x", input_tokens=10)]))

    async def test_a_call_with_no_usage_counts_nothing(self) -> None:
        policy = self._policy()
        for _ in range(20):
            await _observe(policy, "after_model", [_ai("x")])
        self.assertIsNone(policy_result := None or
                          await _observe(policy, "after_model", [_ai("x")]))
        self.assertEqual(policy._spent, 0)

    async def test_no_messages_is_a_no_op(self) -> None:
        self.assertIsNone(await _observe(self._policy(), "after_model", []))


class Usage(unittest.IsolatedAsyncioTestCase):
    class _Store:
        def __init__(self) -> None:
            self.rows: list[Any] = []

        async def add(self, row: Any) -> None:
            self.rows.append(row)

    def _policy(self, store: Any, **kw: Any):
        from yuyutsava.daemon.usage import UsagePolicy

        kw.setdefault("prices", {"m1": (1.0, 2.0)})
        return UsagePolicy(store, role="cli", **kw)

    async def test_one_row_per_model_call(self) -> None:
        store = self._Store()
        policy = self._policy(store, model_name="m1", task_id="t1", thread_id="th1")
        await _observe(policy, "after_model",
                       [_ai("x", input_tokens=1000, output_tokens=500)])
        (row,) = store.rows
        self.assertEqual(
            (row.role, row.model, row.input_tokens, row.output_tokens,
             row.task_id, row.thread_id),
            ("cli", "m1", 1000, 500, "t1", "th1"),
        )

    async def test_cost_is_computed_from_the_price_table(self) -> None:
        store = self._Store()
        await _observe(self._policy(store, model_name="m1"), "after_model",
                       [_ai("x", input_tokens=1_000_000, output_tokens=1_000_000)])
        self.assertAlmostEqual(store.rows[0].est_cost_usd, 3.0, places=6)

    async def test_no_usage_metadata_writes_no_row(self) -> None:
        """A zero row is indistinguishable from a genuinely free call."""
        store = self._Store()
        await _observe(self._policy(store, model_name="m1"), "after_model", [_ai("x")])
        self.assertEqual(store.rows, [])

    async def test_zero_tokens_writes_no_row(self) -> None:
        store = self._Store()
        await _observe(self._policy(store, model_name="m1"), "after_model",
                       [_ai("x", input_tokens=0, output_tokens=0)])
        self.assertEqual(store.rows, [])

    async def test_the_response_model_name_is_the_fallback(self) -> None:
        store = self._Store()
        await _observe(self._policy(store), "after_model",
                       [_ai("x", input_tokens=10, model="claude-from-response")])
        self.assertEqual(store.rows[0].model, "claude-from-response")

    async def test_the_build_time_name_wins(self) -> None:
        store = self._Store()
        await _observe(self._policy(store, model_name="m1"), "after_model",
                       [_ai("x", input_tokens=10, model="claude-from-response")])
        self.assertEqual(store.rows[0].model, "m1")

    async def test_a_store_failure_never_fails_the_turn(self) -> None:
        class _Broken:
            async def add(self, row: Any) -> None:
                raise RuntimeError("db down")

        with self.assertLogs("yuyutsava.daemon.usage", level="ERROR"):
            result = await _observe(self._policy(_Broken(), model_name="m1"),
                                    "after_model", [_ai("x", input_tokens=10)])
        self.assertIsNone(result)


class PromptInspector(unittest.IsolatedAsyncioTestCase):
    def _policy(self):
        from yuyutsava.context.prompt_inspector import PromptInspectorPolicy

        return PromptInspectorPolicy(role="cli")

    async def test_silent_unless_the_env_var_is_set(self) -> None:
        import logging
        import os

        logger = logging.getLogger("yuyutsava.context.prompt_inspector")
        with patch.dict(os.environ, {"YUYUTSAVA_DEBUG_PROMPT": ""}, clear=False):
            with self.assertNoLogs(logger, level="INFO"):
                await _observe(self._policy(), "before_model", [HumanMessage("hi")])

    async def test_reports_when_enabled(self) -> None:
        import os

        with patch.dict(os.environ, {"YUYUTSAVA_DEBUG_PROMPT": "1"}, clear=False):
            with self.assertLogs(
                    "yuyutsava.context.prompt_inspector", level="INFO") as cm:
                await _observe(self._policy(), "before_model",
                               [HumanMessage("hi"), _ai("there")])
        self.assertIn("PROMPT INSPECT [cli]", cm.output[0])
        self.assertIn("2 msgs", cm.output[0])

    async def test_it_never_changes_state(self) -> None:
        import os

        with patch.dict(os.environ, {"YUYUTSAVA_DEBUG_PROMPT": "1"}, clear=False):
            self.assertIsNone(
                await _observe(self._policy(), "before_model", [HumanMessage("hi")]))


class UsageIsResolvedOnce(unittest.IsolatedAsyncioTestCase):
    """Budget and usage read the same ``Turn.usage``; they used to each dig it out."""

    async def test_dict_and_object_metadata_both_work(self) -> None:
        from yuyutsava.policy.adapter import _latest_usage

        class _Obj:
            input_tokens = 7
            output_tokens = 3

        by_dict = _latest_usage([_ai("x", input_tokens=7, output_tokens=3)])
        msg = AIMessage(content="x", id="a2")
        msg.usage_metadata = _Obj()  # type: ignore[assignment]
        by_object = _latest_usage([msg])
        self.assertEqual(
            (by_dict.input_tokens, by_dict.output_tokens),
            (by_object.input_tokens, by_object.output_tokens),
        )

    async def test_the_latest_ai_message_wins(self) -> None:
        from yuyutsava.policy.adapter import _latest_usage

        usage = _latest_usage([
            _ai("old", msg_id="a1", input_tokens=1),
            HumanMessage("mid", id="h1"),
            _ai("new", msg_id="a2", input_tokens=99),
        ])
        self.assertEqual(usage.input_tokens, 99)

    async def test_no_ai_message_yields_none(self) -> None:
        from yuyutsava.policy.adapter import _latest_usage

        self.assertIsNone(_latest_usage([HumanMessage("hi")]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
