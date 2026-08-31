"""Asking the user something, without knowing how the answer arrives.

Phase 4 step 4.3, [ADR-004](../../docs/architecture/review/adr/ADR-004-framework-boundary.md)
item 3, addressing [`F-T06`](../../docs/architecture/review/04-findings-thirdparty-coupling.md#f-t06).

``langgraph.types.interrupt()`` is called from inside domain code — permission
checks, the task-runner gateway, ``tr_ask_user``. Each of those is a YUYUTSAVA
policy decision that happens to need a human; none of them is a LangGraph
concern. The coupling costs one specific thing: **a policy that asks cannot be
tested without a graph**, because ``interrupt()`` only works inside one.

The payloads were already ours — ``yuyutsava.models.interrupts`` is plain
pydantic with no framework import. This makes the *delivery* match, which is why
``ask`` takes the dict those models already produce rather than introducing a
parallel prompt hierarchy.

Two implementations exist:

* ``yuyutsava.policy.ask.LangGraphAskUser`` — calls ``interrupt()``. The only
  place a migrated policy's question reaches the framework.
* ``yuyutsava.policy.ask.ScriptedAskUser`` — returns queued answers, for tests.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class AskUser(Protocol):
    """Put a question to the user and wait for their answer.

    *prompt* is the payload dict a ``yuyutsava.models.interrupts`` model produces
    — it carries a ``type`` discriminator the surfaces dispatch on.

    The return is the user's raw decision string (``"approve"``, free text, …);
    interpreting it is the caller's business, since only the caller knows what it
    asked. Implementations may block for a long time, or never return at all if
    the run is abandoned mid-question.
    """

    async def ask(self, prompt: Mapping[str, Any]) -> str: ...


__all__ = ["AskUser"]
