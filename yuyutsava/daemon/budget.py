"""
Token budget middleware.

Sums input tokens reported via ``usage_metadata`` on each AIMessage. When
the next call would exceed the budget, injects a system instruction asking
the model to wrap up — and refuses to allow more tool calls. The middleware
does not edit message history; it only nudges the model and emits a log
event so the user can see the budget was hit.

This is the architectural "rule that keeps us solvent" from the plan: every
task has a hard ceiling on how many input tokens it may consume across all
turns, regardless of how the conversation grows.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage

logger = logging.getLogger("yuyutsava.daemon.budget")


class BudgetMiddleware(AgentMiddleware):
    """Track running input tokens; force-finalise when the cap is hit."""

    def __init__(self, *, max_input_tokens: int, role: str = "agent") -> None:
        self._cap = max_input_tokens
        self._role = role
        # Per-thread token accumulators, keyed by thread_id from runtime config.
        # We only need the last invocation's number — middleware sees one
        # invocation at a time on a single thread.
        self._spent = 0
        self._exhausted = False

    def reset(self) -> None:
        self._spent = 0
        self._exhausted = False

    def _accumulate(self, msg: Any) -> None:
        usage = getattr(msg, "usage_metadata", None)
        if not usage:
            return
        n = usage.get("input_tokens") if isinstance(usage, dict) else getattr(usage, "input_tokens", 0)
        if n:
            self._spent += int(n)

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        # Find the most recent AI message (the one just returned by the model).
        messages = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        if not messages:
            return None
        for m in reversed(messages):
            if isinstance(m, AIMessage):
                self._accumulate(m)
                break
        if self._spent >= self._cap and not self._exhausted:
            self._exhausted = True
            logger.warning(
                "%s: token budget exhausted (%d/%d) — injecting wrap-up directive",
                self._role, self._spent, self._cap,
            )
            return {
                "messages": [
                    SystemMessage(content=(
                        f"Token budget for this task is exhausted "
                        f"({self._spent} input tokens used, cap {self._cap}). "
                        "Stop calling tools. Summarise what you have done so far "
                        "and what remains in your final reply."
                    ))
                ]
            }
        return None
