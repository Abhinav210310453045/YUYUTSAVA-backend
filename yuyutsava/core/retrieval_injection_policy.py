"""Inject retrieved memory, skills and preferences into the system prompt.

Phase 4 step 4.6, ninth migration (was ``RetrievalInjectionMiddleware``).

Each injector builds one block from the current task text — semantic memory,
matching skills, past conversation, user preferences, board notes — and the
blocks are appended to the system message once per turn.

Two properties the order depends on:

* **Injector order is fixed and meaningful.** The blocks land in the prompt in
  list order, and the prompt is a cached prefix, so reordering them costs cache
  hits on every provider that does prefix caching. The agent fingerprint records
  the chain for that reason.
* **An injector that fails is skipped, not fatal.** Retrieval is an enhancement;
  a dead pgvector connection must degrade the answer, not end the turn.

Only fires when the last message is a human turn — there is nothing to retrieve
*for* mid-tool-loop, and re-running retrieval on every model call inside one turn
would both cost latency and churn the cached prefix.
"""

from __future__ import annotations

from typing import Protocol

from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import ModelCall


class Injector(Protocol):
    async def build_block(self, task_text: str) -> str: ...


class RetrievalInjectionPolicy(Policy):
    """Append top-k memory + skills blocks to the system message once per turn."""

    name = "RetrievalInjectionPolicy"

    def __init__(self, injectors: list[Injector | None]) -> None:
        super().__init__()
        self._injectors = [i for i in injectors if i is not None]

    async def revise_model_call(self, call: ModelCall) -> None:
        if not self._injectors or not call.latest_human_text:
            return
        blocks = await self._blocks(call.latest_human_text)
        if not blocks:
            return
        # One joined block, not several — this reproduces the middleware's
        # single appended block exactly. The leading blank line separates the
        # injection from whatever the prompt already ended with, and is omitted
        # when there is no prompt to separate from: the middleware built a bare
        # `SystemMessage(content=addition)` in that case. Caught by
        # `NoSystemMessage` in test/policy/test_model_call_parity.py, which was
        # the only divergence across 9 policies × 7 request shapes.
        separator = "\n\n" if call.has_system_prompt else ""
        call.append_system_text(separator + "\n\n".join(blocks))

    async def _blocks(self, task_text: str) -> list[str]:
        out: list[str] = []
        for injector in self._injectors:
            try:
                block = await injector.build_block(task_text)
            except Exception:
                block = ""
            if block:
                out.append(block)
        return out


__all__ = ["Injector", "RetrievalInjectionPolicy"]
