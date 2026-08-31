"""The absolute cumulative-spend ceiling for one task.

Phase 4 step 4.8, twelfth migration (was ``BudgetMiddleware``).

Compaction keeps a single prompt inside the model\'s context window; this caps
what a whole task may spend. When the running input-token total crosses the cap,
the model is told — once — to stop calling tools and wrap up. It is a directive
rather than a hard stop because killing the graph mid-tool-call leaves an
orphaned tool call in state, which is a worse failure than one more turn.

Reads :attr:`~yuyutsava.policy.types.Turn.usage`, resolved once by the adapter,
where this and the usage recorder each dug through ``usage_metadata`` themselves.
"""

from __future__ import annotations

import logging

from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import Directive, Turn

logger = logging.getLogger("yuyutsava.daemon.budget_policy")


class BudgetPolicy(Policy):
    """Track running input tokens; force-finalise when the cap is hit."""

    name = "BudgetPolicy"

    def __init__(self, *, max_input_tokens: int, role: str = "agent") -> None:
        super().__init__()
        self._cap = max_input_tokens
        self._role = role
        # Per-invocation accumulator. Middleware sees one invocation at a time on
        # a single thread, so a single counter is enough.
        self._spent = 0
        self._exhausted = False

    def reset(self) -> None:
        self._spent = 0
        self._exhausted = False

    async def after_model(self, turn: Turn) -> Directive | None:
        if not turn.messages:
            return None
        if turn.usage is not None:
            self._spent += turn.usage.input_tokens
        if self._spent < self._cap or self._exhausted:
            return None
        self._exhausted = True
        logger.warning(
            "%s: token budget exhausted (%d/%d) — injecting wrap-up directive",
            self._role, self._spent, self._cap,
        )
        return Directive(
            f"Token budget for this task is exhausted "
            f"({self._spent} input tokens used, cap {self._cap}). "
            "Stop calling tools. Summarise what you have done so far "
            "and what remains in your final reply."
        )


__all__ = ["BudgetPolicy"]
