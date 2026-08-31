"""What the driver needs from a compiled agent — and nothing more.

Phase 4 step 4.5, ADR-004 item 4, partially addressing
[`F-T03`](../../docs/architecture/review/04-findings-thirdparty-coupling.md#f-t03).

``yuyutsava.core.streaming`` annotated its two entrypoints
``agent: CompiledStateGraph``. That is a LangGraph class with a large surface,
of which the driver uses exactly two methods — and naming it made "an agent" and
"a compiled LangGraph graph" the same statement.

:class:`Agent` says what is actually required. ``CompiledStateGraph`` satisfies
it structurally, with no change on its side, and so does the scripted double in
``test/core/test_driver_parity.py`` — which is the point: the driver's whole
test suite runs against something that is not a graph at all.

## What this does NOT do

`F-T03` has two halves. This is the *driving* half. The *constructing* half —
``create_deep_agent`` being the only way this system knows how to build an agent
— is untouched, and wrapping it is a much larger question that ADR-004 files
under Alternative D (migrate off deepagents), explicitly out of scope. Claiming
`F-T03` closed on the strength of this file would be overstating it.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class Agent(Protocol):
    """A compiled agent the streaming driver can run.

    Two methods, because two is what the driver calls.
    """

    def astream(
        self,
        input: Any,
        config: Any = None,
        stream_mode: Any = None,
    ) -> AsyncIterator[Any]:
        """Stream the run. With ``stream_mode=["messages", "updates"]`` this
        yields ``(mode, data)`` tuples; the driver decodes them."""
        ...

    async def aget_state(self, config: Any) -> Any:
        """The thread's checkpointed state, used only to decide whether a
        ``resume=True`` run has anything to resume from."""
        ...


__all__ = ["Agent"]
