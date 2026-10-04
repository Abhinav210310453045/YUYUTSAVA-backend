"""Which model a conversation's turn of one task runs on: its own choice, else the task's.

In order: the session's own choice (``Session.models``) — its model for the
master, the subagent's for a subagent — then the task's model in ``models.toml``
(:class:`~yuyutsava.llm.selection.TaskModels`). A choice naming a model that is
not in ``models.toml`` (removed since it was chosen) is reported and skipped, so
the conversation carries on with the task's model rather than stopping.

The switch that asks this when a turn starts is
:class:`yuyutsava.llm.switch.ModelSwitchMiddleware`; the two meet here, in the
composition layer, because the choice lives in the session store and the switch
sits below storage.
"""

from __future__ import annotations

import logging
from typing import Any

from yuyutsava.llm.tasks import SUBAGENT_PREFIX
from yuyutsava.ports.models import TaskModelSource
from yuyutsava.storage.sessions.store import SessionNotFound, SessionStore

logger = logging.getLogger("yuyutsava.core.model_choice")


class ModelChooser:
    """Which model a conversation's turn of one task runs on: its own choice, else the task's.

    Shared by every agent of a stack; it keeps no state of its own.
    """

    def __init__(self, models: TaskModelSource, sessions: SessionStore | None = None) -> None:
        self.models = models
        self._sessions = sessions

    async def choose(self, thread_id: str, task: str) -> Any:
        """The target *task*'s turn on *thread_id* should run on (a ``ModelTarget``)."""
        reference = await self._chosen(thread_id, task)
        if reference:
            try:
                return self.models.resolve(reference)
            except (KeyError, ValueError) as exc:
                logger.warning(
                    "conversation %s chose model %r, which models.toml does not have (%s); "
                    "using the %s task's model",
                    thread_id,
                    reference,
                    exc,
                    task,
                )
        return self.models.target(task)

    async def _chosen(self, thread_id: str, task: str) -> str:
        """The reference the session chose for *task*, or ``""`` to follow the task."""
        if self._sessions is None or not thread_id:
            return ""
        try:
            choice = (await self._sessions.get(thread_id)).models
        except SessionNotFound:
            return ""  # a thread without a session (an orchestrator task) has no choice
        except Exception:  # noqa: BLE001 — a failed lookup falls back to the task, said aloud
            logger.exception("could not read conversation %s's model choice", thread_id)
            return ""
        if task.startswith(SUBAGENT_PREFIX):
            return choice.for_subagent(task.removeprefix(SUBAGENT_PREFIX))
        return choice.model


__all__ = ["ModelChooser"]
