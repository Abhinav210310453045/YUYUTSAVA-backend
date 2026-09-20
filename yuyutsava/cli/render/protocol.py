"""What the REPL requires of a renderer, stated once.

There are two terminal renderers — Rich on a TTY, ANSI on stderr everywhere
else — and neither is a special case of the other. They share no public
behaviour at all: each draws every event its own way, on its own output. So
what relates them is this protocol and not a base class. A base class here
would hand each renderer the other's helpers, and a fix applied to an
overridden one would be invisible in the renderer that overrode it — the
failure behind `R-BUG-03`, where one renderer drew `image` events and the other
silently dropped them.

The protocol guarantees the *surface* a host may call.
`test/cli/test_renderer_event_kinds.py` guarantees the *coverage*: that each
renderer draws something for every kind.
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
