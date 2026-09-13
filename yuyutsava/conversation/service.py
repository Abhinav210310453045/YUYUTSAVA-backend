"""``ConversationService`` — the I/O-agnostic multi-turn agent loop.

This is the single conversational engine shared by every human↔agent interface:
the terminal CLI REPL (``cli/commands/chat_repl.py``), the Electron text chat,
and the voice agent. It owns *what a conversation is* — a session row, a
``thread_id``, and a turn loop over :func:`astream_agent_iter` — while leaving
*how the human talks to it* (stdin/print, WebSocket text, or STT/TTS audio) to
the caller via two hooks:

  * ``on_event``  — an async sink called once per :class:`StreamEvent`
    (``token`` / ``tool_call`` / ``tool_result`` / ``log`` / ``final``). The
    terminal passes its ``ChatRenderer.render``; the daemon passes a function
    that serializes the event onto a WebSocket; the voice path additionally
    accumulates ``token`` text into the TTS announcer.
  * ``ask_handler`` — an async HITL bridge invoked on graph interrupts
    (permission / question). Same contract as today's CLI ``_ask_handler``.

The service deliberately does **not** build the agent. The caller constructs an
:class:`~yuyutsava.core.engine.AgentBundle` (via
:func:`yuyutsava.cli.agent_stack.build_agent_stack`) with its own settings and
execution mode, then hands it in. That keeps this module free of CLI/daemon
config concerns and lets the daemon reuse its already-running async-subagent
host — so a voice or text conversation delegates background work to the
orchestrator exactly the way the CLI does.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable  # noqa: F401 — Callable used in type strings

from yuyutsava.conversation.titles import derive_session_title
from yuyutsava.core.engine import AgentBundle
from yuyutsava.core.streaming import StreamEvent, astream_agent_iter
from yuyutsava.sessions.runner import (
    _CoalescedTicker,
    _count_memory_files,
    _resolve_session,
)
from yuyutsava.storage.models import Session
from yuyutsava.storage.sessions import SessionStore

logger = logging.getLogger("yuyutsava.conversation.service")

# An async sink for stream events; sync callables are also accepted and called
# directly (the service awaits the result only when it's awaitable).
EventSink = Callable[[StreamEvent], Awaitable[None] | None]
AskHandler = Callable[[object], Awaitable[str]]


class ConversationService:
    """One live multi-turn conversation bound to a session + agent bundle."""

    def __init__(
        self,
        *,
        store: SessionStore,
        session: Session,
        workspace: Path,
        bundle: AgentBundle | None = None,
        bundle_factory: "Callable[[], Awaitable[AgentBundle]] | None" = None,
        agent_path: str = "cli",
        recursion_limit: int = 1000,
        bookkeep: bool = True,
    ) -> None:
        if bundle is None and bundle_factory is None:
            raise ValueError("ConversationService needs a bundle or a bundle_factory")
        self.bundle = bundle
        self._bundle_factory = bundle_factory
        self.store = store
        self.session = session
        self.workspace = workspace
        self.agent_path = agent_path
        self.recursion_limit = recursion_limit
        self._ticker = _CoalescedTicker(store, session.id) if bookkeep else None
        # How many user turns actually ran on this service. Used to discard a
        # freshly-created session that was opened but never used (e.g. the user
        # opened the Chat/Voice tab or the overlay and said nothing) so the
        # Sessions history doesn't fill with empty, seconds-old rows.
        self._turns_ran = 0

    @property
    def turns_ran(self) -> int:
        return self._turns_ran

    async def _ensure_bundle(self) -> AgentBundle:
        """Return the agent bundle, building it lazily on first turn.

        The daemon passes a ``bundle_factory`` so the (heavy) stack build happens
        inside the first ``run_turn`` — which runs in a cancellable task — rather
        than blocking the WS handshake/receive loop. The CLI passes a ready
        ``bundle`` so this is a no-op.
        """
        if self.bundle is None:
            assert self._bundle_factory is not None
            self.bundle = await self._bundle_factory()
        return self.bundle

    @property
    def bundle_ready(self) -> bool:
        return self.bundle is not None

    async def prewarm(self) -> None:
        """Build the agent bundle ahead of the first turn (best-effort).

        The daemon defers the heavy stack build to the first ``run_turn`` so the
        WS handshake stays instant. Calling this right after the handshake — as a
        fire-and-forget task — overlaps that one-time build with the user speaking
        their first utterance, so the first reply isn't a cold start. A no-op once
        the (shared) bundle exists. Never raises.
        """
        if self.bundle is not None:
            return
        try:
            await self._ensure_bundle()
        except Exception:  # noqa: BLE001 — pre-warm is best-effort
            logger.debug("conversation prewarm failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # Construction                                                         #
    # ------------------------------------------------------------------ #

    @classmethod
    async def resolve(
        cls,
        *,
        store: SessionStore,
        workspace: Path,
        bundle: AgentBundle | None = None,
        bundle_factory: "Callable[[], Awaitable[AgentBundle]] | None" = None,
        origin: str = "cli",
        resume_id: str | None = None,
        continue_latest: bool = False,
        agent_path: str = "cli",
        recursion_limit: int = 1000,
        task: str = "(interactive)",
        bookkeep: bool = True,
    ) -> tuple["ConversationService", bool]:
        """Resolve (create/resume) the session row and wrap it in a service.

        Returns ``(service, resuming)`` — ``resuming`` is True when an existing
        session was continued (``--resume`` / ``--continue``), mirroring
        :func:`yuyutsava.sessions.runner._resolve_session`. Pass ``bundle`` for
        an already-built stack (CLI) or ``bundle_factory`` to defer the build to
        the first turn (daemon, so the WS handshake stays instant).
        """
        session, resuming = await _resolve_session(
            store,
            workspace=workspace,
            task=task,
            resume_id=resume_id,
            continue_latest=continue_latest,
            origin=origin,
        )
        svc = cls(
            store=store,
            session=session,
            workspace=workspace,
            bundle=bundle,
            bundle_factory=bundle_factory,
            agent_path=agent_path,
            recursion_limit=recursion_limit,
            bookkeep=bookkeep,
        )
        return svc, resuming

    # ------------------------------------------------------------------ #
    # Accessors                                                            #
    # ------------------------------------------------------------------ #

    @property
    def session_id(self) -> str:
        return self.session.id

    @property
    def thread_id(self) -> str:
        return self.session.thread_id

    # ------------------------------------------------------------------ #
    # Turn loop                                                            #
    # ------------------------------------------------------------------ #

    async def run_turn(
        self,
        text: str,
        *,
        on_event: EventSink,
        ask_handler: AskHandler | None = None,
        run_name: str = "conversation",
        keep_full_payloads: bool = True,
        recursion_limit: int | None = None,
        modality: str = "text",
        resume_value: object | None = None,
    ) -> str:
        """Run one user turn end-to-end, returning the final assistant text.

        Each :class:`StreamEvent` is forwarded to ``on_event`` in arrival order
        (the terminating ``final`` event included, so renderers can close their
        stream). Session bookkeeping (message counter, ``task_preview``) is
        coalesced and flushed once per turn when ``bookkeep`` is on.

        ``resume_value`` answers an interrupt this conversation was parked on
        when a *previous* daemon process died: no new user message is sent, the
        graph simply continues from its checkpoint with that decision. ``text``
        is ignored in that case (and bookkeeping skips the empty preview).
        """
        bundle = await self._ensure_bundle()
        self._turns_ran += 1

        async def _drive(user_text: str | None, resume: object | None) -> tuple[str, int]:
            """One pass of the graph. Returns ``(final_text, steps)``."""
            final_text = ""
            step_count = 0
            async for ev in astream_agent_iter(
                bundle.agent,
                user_text,
                thread_id=self.thread_id,
                recursion_limit=recursion_limit or self.recursion_limit,
                ask_handler=ask_handler,
                run_name=run_name,
                agent_path=self.agent_path,
                keep_full_payloads=keep_full_payloads,
                modality=modality,
                resume_value=resume,
            ):
                if ev.kind == "final":
                    final_text = ev.data.get("text", "") or final_text
                else:
                    step_count += 1
                result = on_event(ev)
                if result is not None:
                    await result
            return final_text, step_count

        final, steps = await _drive(text, resume_value)

        if not final:
            final, extra = await self._recover_empty_turn(
                bundle, on_event=on_event, drive=_drive,
            )
            steps += extra

        if self._ticker is not None and resume_value is None:
            preview = (text or "").strip().replace("\n", " ")
            try:
                await self._ticker.tick(max(steps, 1))
                if preview:
                    await self.store.touch(self.session.id, task_preview=preview)
                if not self.session.title:
                    # Name the session after its first user message — set-once
                    # (the store's WHERE title='' guard keeps it race-safe).
                    title = derive_session_title(text)
                    if title:
                        await self.store.set_title_if_empty(self.session.id, title)
                        self.session = dataclasses.replace(
                            self.session, title=title
                        )
            except Exception:  # noqa: BLE001 — bookkeeping never breaks a turn
                logger.debug("turn bookkeeping failed", exc_info=True)
        return final

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    # Below this age, an unused session survives a disconnect instead of being
    # discarded — long enough for a client to recover from a transient first-
    # connection drop (network blip, panel re-render) and actually resume the
    # SAME session it just cached as its reconnect target, instead of the id
    # being deleted out from under it and every reconnect failing forever.
    _DISCARD_GRACE_SEC = 10.0

    async def discard_if_unused(self) -> bool:
        """Delete this session row iff no turn ever ran on it AND it's past
        the grace window.

        Returns True if the row was discarded. Lets a transport drop a session
        that was opened (tab/overlay connect) but never used, instead of leaving
        an empty row cluttering the Sessions history. Safe: it only deletes a
        session with zero turns — anything the user actually said keeps the
        row. Bookkeeping is skipped for a discarded session.
        """
        if self._turns_ran > 0:
            return False
        if time.time() - self.session.created_at < self._DISCARD_GRACE_SEC:
            return False
        try:
            await self.store.delete(self.session.id)
            return True
        except Exception:  # noqa: BLE001 — cleanup is best-effort
            logger.debug("discard_if_unused: delete failed", exc_info=True)
            return False

    async def _recover_empty_turn(
        self, bundle: AgentBundle, *, on_event: "EventSink", drive: Any,
    ) -> tuple[str, int]:
        """A turn produced nothing. Try to fix it, and never stay silent.

        Measured failure this exists for: a user sent a message while a tool
        call was running, LangGraph fabricated a "cancelled" success result,
        and from then on every call returned ``finish_reason: STOP`` with zero
        output tokens. Four messages, ~70,000 input tokens each, no reply and
        no error — the thread was dead and said nothing about it.

        Two steps, in order:

        1. If the history carries an interrupted tool call, repair it (see
           :mod:`yuyutsava.conversation.repair`) and drive the graph once more
           with no new user message, so the agent answers the message already
           in its history.
        2. Whatever happens, emit a ``notice`` the user can see. Silence is
           indistinguishable from a hang, and it is the one outcome a
           conversational agent must never produce.
        """
        from yuyutsava.conversation.repair import (
            Cause,
            needs_repair,
            repair_orphan_tool_calls,
        )

        steps = 0
        repaired = 0
        try:
            state = await bundle.agent.aget_state(
                {"configurable": {"thread_id": self.thread_id}}
            )
            messages = state.values.get("messages", []) if state and state.values else []
            if needs_repair(messages):
                repaired = await repair_orphan_tool_calls(
                    bundle.agent, self.thread_id,
                    cause=Cause.INTERRUPTED_BY_USER,
                )
        except Exception:  # noqa: BLE001 — recovery must not raise over a reply
            logger.debug("empty-turn inspection failed", exc_info=True)

        if repaired:
            await self._emit(on_event, StreamEvent("notice", {
                "level": "warning",
                "text": (
                    f"The previous turn left {repaired} interrupted tool call"
                    f"{'s' if repaired != 1 else ''} in the history, which the "
                    f"model would not continue from. Repaired — retrying."
                ),
            }))
            # No new user message: theirs is already in the history, and
            # sending it twice would make the model answer itself.
            try:
                final, steps = await drive(None, None)
            except Exception:  # noqa: BLE001
                logger.exception("retry after repair failed")
                final = ""
            if final:
                return final, steps

        await self._emit(on_event, StreamEvent("notice", {
            "level": "error",
            "text": (
                "The model returned an empty response — no text and no tool "
                "call. This usually means the conversation's history is in a "
                "state it will not continue from. Try /new for a fresh "
                "session, or ask again; the last message is still in history."
            ),
        }))
        logger.warning(
            "empty turn on thread=%s (repaired=%d) — surfaced to the user",
            self.thread_id, repaired,
        )
        return "", steps

    @staticmethod
    async def _emit(on_event: "EventSink", ev: StreamEvent) -> None:
        """Forward one synthesised event, tolerating a sink that raises."""
        try:
            result = on_event(ev)
            if result is not None:
                await result
        except Exception:  # noqa: BLE001
            logger.debug("event sink raised on a synthesised notice", exc_info=True)

    async def finish(self, status: str = "done") -> None:
        """Flush final bookkeeping and mark the session ``done``/``crashed``."""
        if self._ticker is not None:
            try:
                await self._ticker.flush_final(
                    memory_files_count=_count_memory_files(self.workspace)
                )
            except Exception:  # noqa: BLE001
                logger.debug("final bookkeeping flush failed", exc_info=True)
        try:
            await self.store.update_status(self.session.id, status)
        except Exception:  # noqa: BLE001
            logger.debug("update_status on finish failed", exc_info=True)

    async def new_session(self, *, task: str = "(interactive)", origin: str = "cli") -> Session:
        """Rotate to a fresh session row + thread_id in-process (CLI ``/new``)."""
        try:
            await self.store.update_status(self.session.id, "done")
        except Exception:  # noqa: BLE001
            pass
        session, _ = await _resolve_session(
            self.store,
            workspace=self.workspace,
            task=task,
            resume_id=None,
            continue_latest=False,
            origin=origin,
        )
        self.session = session
        if self._ticker is not None:
            self._ticker = _CoalescedTicker(self.store, session.id)
        return session
