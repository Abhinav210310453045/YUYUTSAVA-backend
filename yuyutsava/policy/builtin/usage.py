"""The policy that records one ``llm_usage`` row per model call.

The rows, their stores and the aggregates over them live in
:mod:`yuyutsava.storage.usage`; this module only writes them.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from yuyutsava.llm.pricing import estimate_cost_usd, load_price_table
from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import Directive, Turn
from yuyutsava.storage.usage import UsageRow, UsageStore, mint_usage_id

logger = logging.getLogger("yuyutsava.policy.builtin.usage")


class UsagePolicy(Policy):
    """Write one ``llm_usage`` row per model call.

    Constructed per agent build with the routed model's name and provider and
    the task's join keys — the orchestrator builds a fresh graph per task, so
    these are fixed for the policy's lifetime. A conversation that switched model
    (``llm/switch``) is recorded under the model its turn ran on:
    *active* names it.

    Two things it must never do: fail a run, and invent numbers. A failed write
    is logged and swallowed; a call the provider reported no usage for is
    skipped rather than recorded as zero, because a zero row is
    indistinguishable from a genuinely free call when the costs are summed.
    """

    name = "UsagePolicy"

    def __init__(  # noqa: PLR0913  one keyword per llm_usage column it fixes, plus collaborators
        self,
        store: UsageStore,
        *,
        role: str = "agent",
        model_name: str = "",
        provider: str = "",
        task_id: str = "",
        thread_id: str = "",
        prices: dict[str, tuple[float, float]] | None = None,
        active: Callable[[str], Any | None] | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._role = role
        self._model_name = model_name
        self._provider = provider
        self._active = active
        self._task_id = task_id
        self._thread_id = thread_id
        self._prices = prices if prices is not None else load_price_table()

    async def after_model(self, turn: Turn) -> Directive | None:
        usage = turn.usage
        if usage is None or not usage.any_tokens:
            return None
        # The model the turn ran on wins; then the build-time name; `usage.model`
        # is what the provider called it on this particular response, and is the
        # fallback.
        active = self._active(turn.thread_id) if self._active is not None else None
        model = (active.name if active else self._model_name) or usage.model
        provider = active.provider if active else self._provider
        row = UsageRow(
            id=mint_usage_id(),
            ts=time.time(),
            # A pinned thread wins; otherwise take the one the adapter already
            # resolved for this call. The orchestrator can pin because it
            # builds a graph per task, but a chat bundle is shared across
            # every conversation — pinning nothing left the rows thread-less
            # and therefore unattributable to a session.
            thread_id=self._thread_id or turn.thread_id,
            task_id=self._task_id,
            role=self._role,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            # Cost still prices every input token the same. Cached input is
            # cheaper in reality, but inventing a discount would be worse than
            # recording the counts and pricing them once the table is right.
            est_cost_usd=estimate_cost_usd(
                model, usage.input_tokens, usage.output_tokens, self._prices
            ),
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            provider=provider,
        )
        try:
            await self._store.add(row)
        except Exception:
            logger.exception("llm_usage write failed (non-fatal)")
        return None
