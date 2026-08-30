"""Delivering a policy's question — the two ways it can be answered.

Phase 4 step 4.3. See :mod:`yuyutsava.ports.ask` for why the port exists.

:class:`LangGraphAskUser` is one of the few places in the migrated policy layer
that imports the framework at all. That is the whole point: ``interrupt()`` moves
out of the policy and into an adapter, so the policy's *decision* — refuse, ask,
allow — can be exercised without a graph.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable, Mapping


class LangGraphAskUser:
    """Ask via LangGraph's ``interrupt()``.

    ``interrupt()`` is synchronous and works by raising: the first call raises
    ``GraphInterrupt``, which suspends the graph and surfaces the payload to
    whichever surface is driving it; on resume the same call *returns* the
    answer. Wrapping it in an ``async def`` changes none of that — the raise
    propagates out through the await exactly as it did when the middleware called
    it inline.

    The import stays inside the method because this module is imported by policy
    code that must remain loadable without LangGraph installed.
    """

    async def ask(self, prompt: Mapping[str, Any]) -> str:
        from langgraph.types import interrupt

        return interrupt(dict(prompt))


class ScriptedAskUser:
    """Answer from a queue. The reason policies became testable.

    Records every prompt it was given, so a test can assert not just *what the
    policy decided* but *what it asked* — the question text is the part a user
    actually sees, and it was previously unreachable without running a graph.

    Running out of scripted answers raises rather than defaulting: a policy
    asking more questions than the test expected is a behaviour change, and
    silently approving it would be the worst possible default for the one policy
    this was built for.
    """

    def __init__(self, answers: Iterable[str] = ()) -> None:
        self._answers: deque[str] = deque(answers)
        self.asked: list[dict[str, Any]] = []

    async def ask(self, prompt: Mapping[str, Any]) -> str:
        self.asked.append(dict(prompt))
        if not self._answers:
            raise AssertionError(
                f"policy asked {len(self.asked)} question(s) but only "
                f"{len(self.asked) - 1} answer(s) were scripted; the last was: "
                f"{dict(prompt)!r}"
            )
        return self._answers.popleft()


__all__ = ["LangGraphAskUser", "ScriptedAskUser"]
