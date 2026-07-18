"""The ``orch_submit`` tool — a conversation master's hand-off to the daemon.

Events and background work are the orchestrator's job alone: the CLI/chat
master and the TinkerAgent never hold ``ev_*`` tools. When one of them needs
something done (or found out) by the event/background side of the system, it
submits a task here and the daemon's orchestrator runs it like any other
user-approved task — TaskRegistry row, ``GET /tasks`` visibility, HITL asks
routed back to the submitting conversation via ``session_hint``.

Thin wrapper over :class:`~yuyutsava.daemon.task_submission.TaskSubmissionService`
(``submit_direct``: the submitting agent is already acting for the user, so
Tier-1 consent is implicit — exactly the trust level of the authenticated
API). Daemon-only: standalone CLI stacks simply don't register the tool.
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool, tool

from yuyutsava.context.artifacts import thread_id_from_runtime

logger = logging.getLogger("yuyutsava.daemon.submit_tool")


def make_orch_submit_tool(task_submission: object, *, origin: str) -> BaseTool:
    """Build ``orch_submit`` bound to the daemon's TaskSubmissionService.

    ``origin`` names the submitting master ("chat" / "tinker") for the
    registry's audit trail.
    """

    @tool
    async def orch_submit(task: str, context: str | None = None) -> str:
        """Hand a task to the daemon orchestrator to run in the background.

        The orchestrator is the system's brain for events and background
        work — use this to delegate long autonomous jobs, or to ask for
        anything event-related (recent events, proposals, watched-folder
        activity), instead of trying to do those yourself. The task runs
        independently of this conversation; you get back a task id the user
        can follow under Tasks.

        Args:
            task: What the orchestrator should do or find out. Be specific —
                it sees only this text, nothing from this conversation.
            context: Optional extra background worth carrying along
                (appended to the instruction).
        """
        instruction = task.strip()
        if context and context.strip():
            instruction = f"{instruction}\n\nContext from the requesting agent:\n{context.strip()}"
        if not instruction:
            return "[error] task must not be empty"
        # thread_id_from_runtime falls back to the literal "unknown" outside a
        # run — never persist that as a session hint.
        thread_id = thread_id_from_runtime()
        try:
            task_id = await task_submission.submit_direct(  # type: ignore[attr-defined]
                instruction,
                origin=f"agent:{origin}",
                session_hint=thread_id if thread_id not in ("", "unknown") else None,
            )
        except Exception as exc:  # noqa: BLE001 — surface, never crash the turn
            logger.exception("orch_submit failed")
            return f"[error] submission failed: {exc}"
        return (
            f"[ok] submitted to the orchestrator as task {task_id}. It runs in "
            f"the background; completion/asks surface through the daemon."
        )

    return orch_submit
