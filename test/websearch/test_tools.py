"""The ws_* web-search tools: what registers, the rate cap, and the result shape (R-TST-06).

No network: the Tavily client is replaced with a fake module, and the cap with
a recorder. What must hold — a provider with no key registers no tool, a
capped call never reaches the provider and says why, the tool keeps its name
and arguments through the cap wrapper, and a provider failure is a typed error.

Run:  .venv/bin/python -m pytest test/websearch/test_tools.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from unittest import mock

from yuyutsava.config.search import SearchConfig
from yuyutsava.websearch.tools import TavilySearchError, make_search_tools


class _Cap:
    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.asked: list[str] = []

    async def check_and_incr(self, tool_name: str) -> tuple[bool, str]:
        self.asked.append(tool_name)
        return self.allow, "" if self.allow else "10 calls per hour"


def _fake_tavily(*, fail: bool = False):
    """A stand-in ``tavily`` module that records each search."""
    calls: list[dict] = []

    class AsyncTavilyClient:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        async def search(self, **kw):
            calls.append(kw)
            if fail:
                raise RuntimeError("quota exceeded")
            return {
                "answer": "an answer",
                "results": [
                    {"title": "T", "url": "https://u", "content": "c", "score": 0.9, "x": 1}
                ],
            }

    module = types.ModuleType("tavily")
    module.AsyncTavilyClient = AsyncTavilyClient
    return module, calls


def _tavily_tool(**kw):
    (tool,) = make_search_tools(SearchConfig(tavily_api_key="k", exa_api_key=""), **kw)
    return tool


class WhatRegisters(unittest.TestCase):
    def test_no_keys_no_tools(self) -> None:
        self.assertEqual(make_search_tools(SearchConfig(tavily_api_key="", exa_api_key="")), [])

    def test_each_provider_by_its_key(self) -> None:
        names = lambda cfg: sorted(t.name for t in make_search_tools(cfg))  # noqa: E731
        self.assertEqual(
            names(SearchConfig(tavily_api_key="k", exa_api_key="")), ["ws_tavily_search"]
        )
        self.assertEqual(
            names(SearchConfig(tavily_api_key="", exa_api_key="k")),
            ["ws_exa_get_contents", "ws_exa_search"],
        )


class TheCap(unittest.TestCase):
    def test_a_capped_call_never_reaches_the_provider(self) -> None:
        module, calls = _fake_tavily()
        cap = _Cap(allow=False)
        with mock.patch.dict(sys.modules, {"tavily": module}):
            out = json.loads(asyncio.run(_tavily_tool(cap_enforcer=cap).ainvoke({"query": "q"})))
        self.assertEqual(out["error"], "rate_limited")
        self.assertEqual(out["reason"], "10 calls per hour")
        self.assertEqual(calls, [])
        self.assertEqual(cap.asked, ["ws_tavily_search"])

    def test_an_allowed_call_goes_through_with_the_same_surface(self) -> None:
        module, calls = _fake_tavily()
        bare, capped = _tavily_tool(), _tavily_tool(cap_enforcer=_Cap(allow=True))
        self.assertEqual(capped.name, bare.name)
        self.assertEqual(
            capped.args_schema.model_json_schema(), bare.args_schema.model_json_schema()
        )
        with mock.patch.dict(sys.modules, {"tavily": module}):
            asyncio.run(capped.ainvoke({"query": "q"}))
        self.assertEqual(len(calls), 1)


class TavilyResults(unittest.TestCase):
    def test_the_result_shape_and_the_clamped_count(self) -> None:
        module, calls = _fake_tavily()
        with mock.patch.dict(sys.modules, {"tavily": module}):
            out = json.loads(asyncio.run(_tavily_tool().ainvoke({"query": "q", "max_results": 50})))
        self.assertEqual(out["answer"], "an answer")
        self.assertEqual(
            out["results"], [{"title": "T", "url": "https://u", "content": "c", "score": 0.9}]
        )
        self.assertEqual(calls[0]["max_results"], 10)

    def test_a_provider_failure_is_a_typed_error(self) -> None:
        module, _ = _fake_tavily(fail=True)
        with mock.patch.dict(sys.modules, {"tavily": module}):
            with self.assertRaisesRegex(TavilySearchError, "quota exceeded"):
                asyncio.run(_tavily_tool().ainvoke({"query": "q"}))


if __name__ == "__main__":
    unittest.main()
