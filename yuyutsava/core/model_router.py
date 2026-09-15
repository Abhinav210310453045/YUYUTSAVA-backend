"""Complexity-based model routing.

Tasks carry a complexity score 1–5 (triage self-scores organic events; a
cheap light-tier call scores direct API submissions). The router maps the
score to one of three tiers and lazily builds/caches a chat model per tier
via the existing role-env mechanism::

    TIER_LIGHT_LLM_PROVIDER=ollama    TIER_LIGHT_OLLAMA_MODEL=llama3.2:3b
    TIER_STANDARD_LLM_PROVIDER=groq   TIER_STANDARD_GROQ_MODEL=...
    TIER_HEAVY_LLM_PROVIDER=anthropic TIER_HEAVY_ANTHROPIC_MODEL=...

(``llm_settings_from_env("tier_light")`` resolves the prefixed env vars —
zero new config machinery.)

Feature flag ``YUYUTSAVA_MODEL_ROUTING=1``; when OFF (default),
``model_for`` returns the caller-supplied fallback model, i.e. the
existing orchestrator/subagent role models — byte-identical behaviour.
Thresholds come from ``YUYUTSAVA_ROUTING_THRESHOLDS="2,3"``:
complexity ≤ 2 → light, ≤ 3 → standard, else heavy.

Prices live in :mod:`yuyutsava.llm.pricing`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage

from yuyutsava.config.env import env, env_flag
from yuyutsava.llm.factory import chat_model
from yuyutsava.llm.settings import llm_settings_from_env

logger = logging.getLogger("yuyutsava.core.model_router")

ModelTier = Literal["light", "standard", "heavy"]

_DEFAULT_THRESHOLDS = (2, 3)
DEFAULT_COMPLEXITY = 3


def _parse_thresholds(raw: str) -> tuple[int, int]:
    """``"2,3"`` → ``(2, 3)``; anything malformed falls back to the default."""
    try:
        parts = [int(p) for p in raw.split(",")]
        if len(parts) == 2 and 1 <= parts[0] <= parts[1] <= 5:
            return (parts[0], parts[1])
    except ValueError:
        pass
    logger.warning(
        'YUYUTSAVA_ROUTING_THRESHOLDS=%r invalid (want e.g. "2,3") — using %s',
        raw,
        _DEFAULT_THRESHOLDS,
    )
    return _DEFAULT_THRESHOLDS


@dataclass(frozen=True)
class RoutingSettings:
    """Env-derived routing config."""

    enabled: bool = False  # YUYUTSAVA_MODEL_ROUTING
    light_max: int = 2  # YUYUTSAVA_ROUTING_THRESHOLDS="<light>,<standard>"
    standard_max: int = 3

    @classmethod
    def from_env(cls) -> RoutingSettings:
        enabled = env_flag("YUYUTSAVA_MODEL_ROUTING", true=("1", "true", "yes"))
        raw = env("YUYUTSAVA_ROUTING_THRESHOLDS")
        light_max, standard_max = _parse_thresholds(raw) if raw else _DEFAULT_THRESHOLDS
        return cls(enabled=enabled, light_max=light_max, standard_max=standard_max)


class ModelRouter:
    """Maps task complexity to a tier-appropriate chat model.

    Tier models are built lazily on first use (via ``chat_model(
    llm_settings_from_env("tier_<tier>"))``) and cached for the router's
    lifetime — the orchestrator builds a fresh graph per task, so one
    router instance serves the whole daemon.
    """

    def __init__(self, settings: RoutingSettings | None = None) -> None:
        self._settings = settings or RoutingSettings.from_env()
        self._cache: dict[ModelTier, BaseChatModel] = {}

    @classmethod
    def from_env(cls) -> ModelRouter:
        return cls(RoutingSettings.from_env())

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    def tier_for(self, complexity: int | None) -> ModelTier:
        """Threshold mapping; None/out-of-range scores clamp to 1–5 around 3."""
        c = DEFAULT_COMPLEXITY if complexity is None else max(1, min(int(complexity), 5))
        if c <= self._settings.light_max:
            return "light"
        if c <= self._settings.standard_max:
            return "standard"
        return "heavy"

    def tier_model(self, tier: ModelTier) -> BaseChatModel:
        """Build (or return the cached) chat model for one tier.

        Raises when the tier's provider env is misconfigured (e.g. missing
        API key) — :meth:`model_for` catches and falls back.
        """
        cached = self._cache.get(tier)
        if cached is not None:
            return cached
        model = chat_model(llm_settings_from_env(f"tier_{tier}"), temperature=0.0)
        self._cache[tier] = model
        return model

    def model_for(self, complexity: int | None, *, fallback: BaseChatModel) -> BaseChatModel:
        """The model a task of this complexity should run on.

        Routing disabled → ``fallback`` (the existing role model), exactly
        the pre-Phase-4 behaviour. A misconfigured tier also falls back —
        routing must never make a runnable task unrunnable.
        """
        if not self._settings.enabled:
            return fallback
        tier = self.tier_for(complexity)
        try:
            return self.tier_model(tier)
        except Exception:
            logger.exception("tier %r model unavailable — falling back to the role model", tier)
            return fallback


# ---------------------------------------------------------------------------
# Complexity scoring for direct submissions (which skip triage)
# ---------------------------------------------------------------------------


_SCORE_PROMPT = """\
Rate the complexity of this task for an AI agent on a 1-5 scale:
1 = trivial single action (move one file)
2 = simple batch (rename a batch of files)
3 = moderate (summarize a document)
4 = multi-step research (web search + synthesis)
5 = complex engineering (build/refactor code across files)

Reply with ONLY the digit.

Task: {instruction}
"""

_DIGIT_RE = re.compile(r"[1-5]")


class ComplexityScorer:
    """One short light-tier completion → an int 1–5; **never** raises.

    The model is resolved lazily via ``model_factory`` (typically
    ``lambda: router.tier_model("light")``) so a misconfigured light tier
    degrades to the default score instead of breaking daemon boot. Any
    failure — factory, LLM call, unparseable reply — scores
    ``DEFAULT_COMPLEXITY`` (3), per the plan's "never block on scoring".
    """

    def __init__(self, model_factory: Callable[[], BaseChatModel]) -> None:
        self._factory = model_factory
        self._model: BaseChatModel | None = None

    async def score(self, instruction: str) -> int:
        try:
            if self._model is None:
                self._model = self._factory()
            reply = await self._model.ainvoke(
                [HumanMessage(content=_SCORE_PROMPT.format(instruction=instruction[:2000]))]
            )
            text = reply.content if isinstance(reply.content, str) else str(reply.content)
            m = _DIGIT_RE.search(text)
            if m:
                return int(m.group(0))
            logger.warning(
                "complexity scorer: unparseable reply %r — using %d", text[:80], DEFAULT_COMPLEXITY
            )
        except Exception as exc:
            logger.warning("complexity scorer failed (%s) — using %d", exc, DEFAULT_COMPLEXITY)
        return DEFAULT_COMPLEXITY
