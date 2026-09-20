"""What the REPL requires of a renderer, stated once.

There are two terminal renderers — Rich on a TTY, ANSI on stderr everywhere
else — and they used to be related by inheritance. That looked like sharing but
was not: the rich renderer overrides every single public method, so it
inherited none of the behaviour and all of the private helpers, including
fifteen it had already replaced with its own. A fix applied to one of those was
invisible in the other, which is how the plain renderer came to drop `image`
events (`R-BUG-03`).

A protocol says what a host may call without implying that either renderer is
a special case of the other. Both satisfy it structurally, so neither imports
the other, and `test/cli/test_renderer_event_kinds.py` is what keeps their
*coverage* in step — the protocol only guarantees the surface.
"""

from __future__ import annotations

from typing import Any, ContextManager, Protocol, runtime_checkable

from yuyutsava.cli.render.ring import PayloadRing
from yuyutsava.core.streaming import StreamEvent


@runtime_checkable
class ChatRendererProtocol(Protocol):
    """One turn's worth of display, as the REPL and the dashboard use it."""

    #: The captured payloads behind `/ring`, `/last` and `/expand`. A host talks
    #: to it directly rather than through three delegating methods per renderer.
    ring: PayloadRing

    #: Repaint hook for a host that draws its own status bar (the dashboard
    #: sets it). ``None`` when nobody is listening.
    on_change: Any

    def begin_turn(self) -> None:
        """A turn is starting: show whatever says "working"."""

    async def render(self, ev: StreamEvent) -> None:
        """Display one streamed event. Every kind must produce some output."""

    async def end_of_turn(self) -> None:
        """The turn is over: flush and close anything still open."""

    def status_text(self) -> str:
        """What the agent is doing, as plain text, or ``""`` if not tracked."""

    def tool_in_flight(self) -> str:
        """The tool currently running, or ``""``."""

    def note_retry(
        self, model: str, attempt: int, retries: int, delay: float, exc: BaseException
    ) -> None:
        """Report a provider retry in this renderer's own voice."""

    def pause(self) -> ContextManager[Any]:
        """Stop drawing for the block's duration, so a prompt can own the tty."""


__all__ = ["ChatRendererProtocol"]
