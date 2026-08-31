"""ModelRouter: tier mapping, threshold parsing, flag-off passthrough,
lazy build + cache, misconfigured-tier fallback, price table, scorer.

Run:  uv run python -m unittest test.core.test_model_router -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage

from yuyutsava.core.model_router import (
    DEFAULT_COMPLEXITY,
    ComplexityScorer,
    ModelRouter,
    RoutingSettings,
    estimate_cost_usd,
    load_price_table,
)


class RoutingSettingsTests(unittest.TestCase):
    def test_defaults_disabled(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            s = RoutingSettings.from_env()
        self.assertFalse(s.enabled)
        self.assertEqual((s.light_max, s.standard_max), (2, 3))

    def test_enabled_and_custom_thresholds(self) -> None:
        env = {"YUYUTSAVA_MODEL_ROUTING": "1", "YUYUTSAVA_ROUTING_THRESHOLDS": "1,4"}
        with mock.patch.dict("os.environ", env, clear=True):
            s = RoutingSettings.from_env()
        self.assertTrue(s.enabled)
        self.assertEqual((s.light_max, s.standard_max), (1, 4))

    def test_malformed_thresholds_fall_back(self) -> None:
        for raw in ("nope", "3", "3,2", "0,3", "2,9", "1,2,3"):
            env = {"YUYUTSAVA_ROUTING_THRESHOLDS": raw}
            with mock.patch.dict("os.environ", env, clear=True):
                s = RoutingSettings.from_env()
            self.assertEqual((s.light_max, s.standard_max), (2, 3), raw)


class TierMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = ModelRouter(RoutingSettings(enabled=True))

    def test_default_thresholds(self) -> None:
        self.assertEqual(self.router.tier_for(1), "light")
        self.assertEqual(self.router.tier_for(2), "light")
        self.assertEqual(self.router.tier_for(3), "standard")
        self.assertEqual(self.router.tier_for(4), "heavy")
        self.assertEqual(self.router.tier_for(5), "heavy")

    def test_none_and_out_of_range_clamp(self) -> None:
        self.assertEqual(self.router.tier_for(None), "standard")  # default 3
        self.assertEqual(self.router.tier_for(0), "light")        # clamps to 1
        self.assertEqual(self.router.tier_for(99), "heavy")       # clamps to 5


class ModelForTests(unittest.TestCase):
    def test_flag_off_returns_fallback_identity(self) -> None:
        router = ModelRouter(RoutingSettings(enabled=False))
        fallback = object()
        for c in (1, 3, 5, None):
            self.assertIs(router.model_for(c, fallback=fallback), fallback)

    def test_flag_on_builds_tier_model_lazily_and_caches(self) -> None:
        # Ollama needs no API key, so the tier model builds without
        # secrets or network (ChatOpenAI construction is offline).
        env = {"TIER_LIGHT_LLM_PROVIDER": "ollama", "TIER_LIGHT_OLLAMA_MODEL": "tiny:1b"}
        router = ModelRouter(RoutingSettings(enabled=True))
        with mock.patch.dict("os.environ", env, clear=True):
            m1 = router.model_for(1, fallback=object())
            m2 = router.model_for(2, fallback=object())
        self.assertIs(m1, m2)  # cached per tier
        self.assertEqual(m1.model_name, "tiny:1b")

    def test_misconfigured_tier_falls_back(self) -> None:
        # anthropic without ANTHROPIC_API_KEY raises inside tier_model;
        # model_for must catch and return the fallback.
        env = {"TIER_HEAVY_LLM_PROVIDER": "anthropic"}
        router = ModelRouter(RoutingSettings(enabled=True))
        fallback = object()
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertIs(router.model_for(5, fallback=fallback), fallback)


class PriceTableTests(unittest.TestCase):
    def test_longest_prefix_wins(self) -> None:
        prices = {"gpt-4o": (2.50, 10.00), "gpt-4o-mini": (0.15, 0.60)}
        cost = estimate_cost_usd("gpt-4o-mini-2024", 1_000_000, 1_000_000, prices)
        self.assertAlmostEqual(cost, 0.15 + 0.60)

    def test_unknown_model_costs_zero(self) -> None:
        self.assertEqual(estimate_cost_usd("never-heard-of-it", 10**6, 10**6, {}), 0.0)

    def test_known_token_counts_sum_correctly(self) -> None:
        prices = {"m": (1.00, 5.00)}
        # 200k in + 40k out @ $1/$5 per 1M = 0.2 + 0.2
        self.assertAlmostEqual(
            estimate_cost_usd("m", 200_000, 40_000, prices), 0.4
        )

    def test_override_file_merges_over_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "model_prices.json").write_text(
                json.dumps({"claude-opus-4": [9.0, 99.0], "my-model": [1.0, 2.0]})
            )
            with mock.patch(
                "yuyutsava.core.model_router.state_dir", return_value=Path(tmp)
            ):
                table = load_price_table()
        self.assertEqual(table["claude-opus-4"], (9.0, 99.0))   # file wins
        self.assertEqual(table["my-model"], (1.0, 2.0))         # file extends
        self.assertIn("gpt-4o-mini", table)                     # defaults kept

    def test_malformed_override_file_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "model_prices.json").write_text("{not json")
            with mock.patch(
                "yuyutsava.core.model_router.state_dir", return_value=Path(tmp)
            ):
                table = load_price_table()
        self.assertIn("gpt-4o-mini", table)


class _ScoreModel:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return AIMessage(content=self.reply)


class _ExplodingModel:
    async def ainvoke(self, messages):
        raise RuntimeError("llm down")


class ComplexityScorerTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_digit(self) -> None:
        model = _ScoreModel("4")
        self.assertEqual(await ComplexityScorer(lambda: model).score("research X"), 4)

    async def test_parses_digit_embedded_in_prose(self) -> None:
        model = _ScoreModel("I'd rate this a 2.")
        self.assertEqual(await ComplexityScorer(lambda: model).score("rename"), 2)

    async def test_unparseable_reply_defaults(self) -> None:
        model = _ScoreModel("hard to say")
        self.assertEqual(
            await ComplexityScorer(lambda: model).score("x"), DEFAULT_COMPLEXITY
        )

    async def test_llm_failure_defaults(self) -> None:
        scorer = ComplexityScorer(lambda: _ExplodingModel())
        self.assertEqual(await scorer.score("x"), DEFAULT_COMPLEXITY)

    async def test_factory_failure_defaults(self) -> None:
        def boom():
            raise RuntimeError("tier misconfigured")
        self.assertEqual(await ComplexityScorer(boom).score("x"), DEFAULT_COMPLEXITY)

    async def test_model_resolved_once(self) -> None:
        model = _ScoreModel("3")
        built = 0

        def factory():
            nonlocal built
            built += 1
            return model

        scorer = ComplexityScorer(factory)
        await scorer.score("a")
        await scorer.score("b")
        self.assertEqual(built, 1)
        self.assertEqual(model.calls, 2)


if __name__ == "__main__":
    unittest.main()
