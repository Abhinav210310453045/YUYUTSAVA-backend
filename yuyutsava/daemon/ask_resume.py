"""Re-entry for asks answered after the process that raised them is gone.

An ask blocks its agent on a LangGraph ``interrupt()`` — and LangGraph has
already *checkpointed* the graph at that point. So the agent surviving a daemon
restart is not the hard part; the hard part is that the ``asyncio.Future`` the
channel was awaiting died with the old process. Answering such an ask therefore
can't wake a waiter: it has to re-enter the graph.

That is the whole job of this service. :class:`DecisionService` calls it when a
response arrives for an ask with no live waiter, and it routes by the ownership
recorded on the ask:

* a conversation (chat / voice / tinker) → a detached run on the Phase 2
  :class:`~yuyutsava.daemon.turn_registry.TurnRegistry` that re-enters with
  ``Command(resume=<decision>)``, so the answer streams to whoever is watching
  that thread;
* a background task → the watcher's own ``runs.create(command={"resume": …})``
  against the subagent host, which is the same path a live reply takes.

Anything it can't route reports failure rather than silently swallowing the
answer, so the caller can surface a real conflict instead of pretending.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("yuyutsava.daemon.ask_resume")

# Surfaces whose owner is a conversation thread the daemon can re-enter itself.
_CONVERSATION_SURFACES = frozenset({"chat", "voice", "tinker", "cli"})


class AskResumeService:
    """Delivers a late answer to the agent that is still waiting for it."""

    def __init__(
        self,
        *,
        registry: Any,                       # daemon.ask_registry.AskRegistry
        conversation_manager: Any = None,    # daemon.conversation_manager.ConversationManager
        watcher: Any = None,                 # async_subagents.watcher.AsyncTaskHealthWatcher
        channels: Any = None,                # daemon.channels.ChannelRouter
    ) -> None:
        self._registry = registry
        self._conversations = conversation_manager
        self._watcher = watcher
        self._channels = channels

    async def resume(self, ask_id: str, response: str) -> bool:
        """Deliver ``response`` to the agent parked on ``ask_id``.

        Returns False when the ask is unknown, already answered, or belongs to
        an owner this daemon can't reach — never raises into the responder.
        """
        record = self._registry.get(ask_id)
        if record is None:
            return False
        if not self._registry.is_orphaned(ask_id):
            # A live waiter exists (or existed) for this one — the caller's
            # normal future path owns it; resuming as well would double-answer.
            return False

        surface = str(record.get("surface") or "background")
        try:
            if record.get("task_id") and self._watcher is not None:
                ok = await self._resume_background(record, response)
            elif surface in _CONVERSATION_SURFACES and record.get("thread_id"):
                ok = await self._resume_conversation(record, response)
            else:
                logger.warning(
                    "ask %s (surface=%s) has no owner this daemon can re-enter",
                    ask_id, surface,
                )
                ok = False
        except Exception:  # noqa: BLE001 — a failed resume must not 500 the responder
            logger.exception("resuming ask %s failed", ask_id)
            ok = False

        if ok:
            await self._registry.resolve(ask_id, response)
        return ok

    # ------------------------------------------------------------------ #
    # Owners                                                              #
    # ------------------------------------------------------------------ #

    async def _resume_background(self, record: dict[str, Any], response: str) -> bool:
        return await self._watcher.resume_interrupt(
            str(record["task_id"]), record.get("interrupt_id"), response,
        )

    async def _resume_conversation(self, record: dict[str, Any], response: str) -> bool:
        """Start a detached run that re-enters the thread with the decision."""
        manager = self._conversations
        if manager is None:
            return False

        thread_id = str(record["thread_id"])
        surface = str(record.get("surface") or "chat")
        card_id = record.get("card_id")
        agent = "tinker" if (surface == "tinker" and card_id) else "master"

        convo, _resuming = await manager.open(
            agent=agent,
            card_id=card_id,
            origin=surface if surface != "cli" else "cli",
            resume_id=thread_id,
        )

        # Any *further* interrupt this continuation raises has to become a
        # durable ask too, so it goes back through the same hub path.
        ask_handler = None
        if self._channels is not None:
            from yuyutsava.daemon.orchestrator_loop import make_ask_handler
            ask_handler = make_ask_handler(
                self._channels,
                default_session_id=thread_id,
                default_agent_path=surface,
                surface=surface,
                default_agent_label=record.get("agent_label") or surface,
            )

        # A single interrupt resumes with a scalar; the keyed form is only
        # required when a turn was parked on several at once, and we only know
        # the id of the one being answered.
        resume_value: Any = response
        interrupt_id = record.get("interrupt_id")
        if interrupt_id:
            resume_value = {str(interrupt_id): response}

        async def _body(run) -> None:
            async def _sink(ev) -> None:
                run.emit({"type": ev.kind, **ev.data})
            await convo.run_turn(
                "",
                on_event=_sink,
                ask_handler=ask_handler,
                run_name=f"{surface}-resume",
                keep_full_payloads=True,
                resume_value=resume_value,
            )

        run = manager.start_turn(
            thread_id,
            body=_body,
            session_id=convo.session_id,
            origin=surface,
            agent=agent,
            card_id=card_id,
            kind="text",
            text="",
        )
        if run is None:
            # A turn is already executing on this thread — that means the
            # process never actually died and something else owns the answer.
            logger.warning(
                "ask resume for %s skipped: a turn is already running", thread_id,
            )
            return False
        logger.info(
            "ask resume: re-entered %s (run %s) with a stored decision",
            thread_id, run.run_id,
        )
        return True
