"""Failures that matter are logged, not turned silently into defaults (R-ERR-02).

Each site below caught everything and carried on with an empty value and no
log: a preferences store that failed on every turn, a tool published to the
model as taking no parameters, a programming error reported as "could not be
parsed", a bug in the price refresh hidden at DEBUG. Each still degrades the
same way; now it says so, or — for the programming error — it surfaces.

Run:  .venv/bin/python -m pytest test/core/test_failures_are_visible.py
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from yuyutsava.config.limits import LIMITS
from yuyutsava.core import agent_stack, tool_result
from yuyutsava.discovery.tool_registry import ToolRegistry
from yuyutsava.policy.builtin.retrieval_injection import RetrievalInjectionPolicy


class _Injector:
    def __init__(self, block: str) -> None:
        self._block = block

    async def build_block(self, task_text: str) -> str:
        return self._block


class _BrokenInjector:
    async def build_block(self, task_text: str) -> str:
        raise RuntimeError("prefs store is gone")


class AFailingInjector(unittest.IsolatedAsyncioTestCase):
    async def test_is_logged_by_name_and_the_others_still_land(self) -> None:
        policy = RetrievalInjectionPolicy([_Injector("A"), _BrokenInjector(), _Injector("B")])
        with self.assertLogs("yuyutsava.policy.builtin.retrieval_injection", "WARNING") as logs:
            blocks = await policy._blocks("hello")
        self.assertEqual(blocks, ["A", "B"])
        self.assertIn("_BrokenInjector failed", logs.output[0])
        self.assertIsNotNone(logs.records[0].exc_info)


class ASchemaThatCannotRender(unittest.TestCase):
    def test_is_logged_by_tool_name(self) -> None:
        def boom() -> dict:
            raise TypeError("not JSON-schema-able")

        tool = SimpleNamespace(
            name="odd_tool", description="d", args_schema=SimpleNamespace(model_json_schema=boom)
        )
        with self.assertLogs("yuyutsava.discovery.tool_registry", "WARNING") as logs:
            block = json.loads(ToolRegistry().schema_block([tool]))
        self.assertEqual(block[0]["parameters"], {})
        self.assertIn("'odd_tool'", logs.output[0])


class AnOversizedToolResult(unittest.TestCase):
    big = "x" * (LIMITS.max_tool_result_chars + 10)

    def test_output_that_is_not_json_is_still_a_parse_notice(self) -> None:
        out = json.loads(tool_result.guard_tool_result("plain " + self.big, "tr_read_file"))
        self.assertTrue(out["suppressed"])
        self.assertIn("could not be parsed", out["notice"]["human_message"])

    def test_a_bug_building_the_notice_is_not_reported_as_a_parse_failure(self) -> None:
        content = json.dumps({"result": {"stdout": self.big}})
        with (
            mock.patch.object(
                tool_result.SuppressedContentNotice,
                "stdout_too_large",
                side_effect=AttributeError("a bug"),
            ),
            self.assertRaisesRegex(AttributeError, "a bug"),
        ):
            tool_result.guard_tool_result(content, "tr_execute_in_sandbox")


class APriceRefreshBug(unittest.IsolatedAsyncioTestCase):
    async def test_is_a_warning_not_debug(self) -> None:
        """``refresh_price_cache`` handles its own failures; anything escaping it
        is a bug, and must not be hidden at DEBUG."""
        with (
            mock.patch.object(agent_stack, "refresh_price_cache", side_effect=KeyError("bug")),
            self.assertLogs("yuyutsava.core.agent_stack", "WARNING") as logs,
        ):
            await agent_stack._refresh_prices(SimpleNamespace())
        self.assertIn("price cache refresh failed", logs.output[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
