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

    #: Where provisional rows go: the part of an answer that has arrived but
    #: is not yet a complete markdown block. A host that can redraw rows in
    #: place sets it; ``None`` means the renderer keeps the partial text to
    #: itself until the block closes. A data member for the same reason
    #: ``on_change`` is one — every renderer declares it, so a host can assign
    #: without asking which renderer it got.
    show_pending: Any

    def user_message(self, text: str, *, when: str = "") -> None:
        """Put the line the user typed into the transcript.

        On the protocol because the REPL calls it: the renderer owns where a
        speaker's marker goes and which colour names them, and the REPL has no
        business knowing either. Before this existed nothing printed the user's
        own messages at all.
        """

    def skills_recalled(self, names: tuple[str, ...]) -> None:
        """Name the skills retrieval matched to this turn.

        On the protocol because the REPL subscribes to the retrieval layer and
        hands the names here: a log record reaches the transcript pane without
        passing through a renderer, so it arrives unindented and uncoloured in
        the middle of a turn.
        """

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

    async def aclose(self) -> None:
        """Release anything still buffering output. Called once, at shutdown.

        In the protocol because the alternative is what the REPL used to do:
        reach for ``renderer._smoother``, a field only one of the two renderers
        has.
        """


__all__ = ["ChatRendererProtocol"]
