"""Health watcher + HITL bridge for async subagent runs.

deepagents' ``AsyncSubAgentMiddleware`` only starts background runs and lets
the master poll their status via ``check_async_task``. It does **not** watch
runs for ``interrupt()`` events or surface them to the user. The watcher
closes that gap.

Responsibilities
----------------
1. **Discovery**: every poll cycle, list threads on the Agent Protocol server
   and add any new ones to the mirror. Tasks the master starts via
   ``start_async_task`` thus show up automatically — we don't need a
   middleware hook on the master side.
2. **Status tracking**: for each non-terminal task in the mirror, call
   ``runs.get`` and update the mirror on status changes. Post
   ``AsyncTask*Payload`` channel events as appropriate.
3. **HITL bridge**: when a run's status flips to ``interrupted``, fetch the
   thread's ``interrupts`` dict, route each ``Interrupt`` through the same
   ``ask_handler`` the daemon's sync flow uses, then resume the run by
   creating a new run on the same thread with ``command={"resume": reply,
   "resumable": True}``.
4. **Lifecycle**: cancel hung tasks past their wall-clock timeout; cancel all
   non-terminal tasks on daemon shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from yuyutsava.daemon.channels import (
    AskPrompt,
    AsyncTaskAwaitingUserPayload,
    AsyncTaskCompletedPayload,
    AsyncTaskProgressPayload,
    AsyncTaskStartedPayload,
    ChannelEvent,
)
from yuyutsava.async_subagents.mirror import (
    AsyncTaskMirror,
    MirroredTask,
    TERMINAL_STATUSES,
)

logger = logging.getLogger("yuyutsava.async_subagents.watcher")


AskHandler = Callable[[AskPrompt], Awaitable[str]]
EventSink = Callable[[ChannelEvent], Awaitable[None]]


# Statuses we treat as "still running" for HITL/polling purposes.
_NON_TERMINAL = frozenset({"pending", "running"})


def _first_str(*parts: str | None) -> str:
    for p in parts:
        if p:
            return p
    return ""


def _extract_thread_id(thread_obj: Any) -> str | None:
    if isinstance(thread_obj, dict):
        return thread_obj.get("thread_id")
    return getattr(thread_obj, "thread_id", None)


def _last_message_text(values: Any) -> str:
    """Best-effort extraction of the final assistant message text.

    Thread values come back as a dict with ``messages: list[Message]``.
    Each message may be a dict (``{"role": ..., "content": ...}``) or an
    object with ``.content``. Content may be a string or a list of blocks.
    """
    if not isinstance(values, dict):
        return ""
    msgs = values.get("messages") or []
    if not msgs:
        return ""
    last = msgs[-1]
    content = last.get("content") if isinstance(last, dict) else getattr(last, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                out.append(str(block["text"]))
            elif isinstance(block, str):
                out.append(block)
        return "\n".join(out)
    return str(content) if content is not None else ""


class AsyncTaskHealthWatcher:
    """Polls runs, mirrors state, and bridges interrupts to the user.

    One instance per daemon (or CLI session). Single asyncio task.

    Parameters
    ----------
    mirror:
        Daemon-scoped task mirror; mutated as the watcher observes runs.
    host_url:
        Base URL of the local Agent Protocol server (the AsyncSubagentHost).
        Remote-hosted subagents are tracked separately in v1 (out of scope).
    ask_handler:
        Same callable produced by ``yuyutsava.daemon.orchestrator_loop._make_ask_handler``.
        Routes an ``AskPrompt`` through the daemon's ``ChannelRouter``.
    event_sink:
        Sink for ``ChannelEvent`` updates. Daemon: ``ChannelRouter.post_event``.
        CLI: ``CliHitlBridge.post_event``.
    agent_path_root:
        Prefix for the ``agent_path`` on emitted asks. ``"orchestrator"`` in the
        daemon, ``"cli"`` for the CLI deepagent. The watcher always appends
        ``#bg`` so the UI can tag bg interrupts distinctly.
    poll_interval_sec:
        How often to refresh status (default 1.5s).
    per_task_timeout_sec:
        Wallclock limit per task; runs exceeding this are cancelled (default 1h).
    """

    def __init__(
        self,
        *,
        mirror: AsyncTaskMirror,
        host_url: str,
        ask_handler: AskHandler,
        event_sink: EventSink,
        agent_path_root: str = "orchestrator",
        poll_interval_sec: float = 1.5,
        per_task_timeout_sec: float = 3600.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._mirror = mirror
        self._host_url = host_url
        self._ask = ask_handler
        self._emit = event_sink
        self._agent_path_root = agent_path_root
        self._poll = poll_interval_sec
        self._task_timeout = per_task_timeout_sec
        self._headers = headers
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._client = None
        self._known_threads: set[str] = set()
        # task_id -> set of interrupt_ids already routed (avoid double-ask on slow resume)
        self._handled_interrupts: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        from langgraph_sdk import get_client
        self._client = get_client(url=self._host_url, headers=self._headers)
        self._stop.clear()
        self._loop_task = asyncio.create_task(self._run_loop(), name="async-task-watcher")

    async def shutdown(self) -> None:
        self._stop.set()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(self._loop_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._loop_task.cancel()
                try:
                    await self._loop_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._loop_task = None
        # Best-effort: cancel every non-terminal run on the server.
        if self._client is not None:
            for t in self._mirror.list_non_terminal():
                if not t.sub_thread_id:
                    continue
                try:
                    runs = await self._client.runs.list(thread_id=t.sub_thread_id, limit=5)
                    for r in runs or []:
                        rid = r.get("run_id") if isinstance(r, dict) else getattr(r, "run_id", None)
                        if not rid:
                            continue
                        try:
                            await self._client.runs.cancel(thread_id=t.sub_thread_id, run_id=rid)
                        except Exception:  # noqa: BLE001
                            pass
                except Exception:  # noqa: BLE001
                    pass
        await self._mirror.mark_all_cancelled(reason="daemon_shutdown")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self._discover_new_threads()
                    await self._poll_known_tasks()
                except Exception:
                    logger.exception("watcher cycle failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
                except asyncio.TimeoutError:
                    pass
        finally:
            logger.debug("watcher loop exiting")

    async def _discover_new_threads(self) -> None:
        """List threads on the server and ingest any we don't know yet."""
        assert self._client is not None
        try:
            threads = await self._client.threads.search(limit=50)
        except Exception:
            logger.debug("threads.search failed (continuing)", exc_info=True)
            return
        for th in threads or []:
            tid = _extract_thread_id(th)
            if not tid or tid in self._known_threads:
                continue
            self._known_threads.add(tid)
            await self._ingest_thread_runs(tid)

    async def _ingest_thread_runs(self, thread_id: str) -> None:
        """For a freshly-discovered thread, mirror its non-terminal runs."""
        assert self._client is not None
        try:
            runs = await self._client.runs.list(thread_id=thread_id, limit=5)
        except Exception:
            return
        for r in runs or []:
            run_dict = r if isinstance(r, dict) else r.__dict__
            run_id = run_dict.get("run_id")
            status = run_dict.get("status") or "running"
            if not run_id:
                continue
            # Use thread_id as the task_id (matches deepagents'
            # AsyncSubAgentMiddleware convention — see middleware/async_subagents.py).
            task_id = thread_id
            if self._mirror.get(task_id) is not None:
                continue
            # We don't know the agent_name from runs.list alone; try to read
            # it from thread metadata if available, otherwise mark unknown.
            agent_name = await self._guess_agent_name(thread_id, run_dict) or "unknown-bg"
            now = time.time()
            await self._mirror.upsert(MirroredTask(
                task_id=task_id,
                agent_name=agent_name,
                graph_id=run_dict.get("assistant_id") or "",
                instruction="(discovered)",
                status=status,
                started_at=now,
                last_update_at=now,
                sub_thread_id=thread_id,
            ))
            await self._emit(ChannelEvent(payload=AsyncTaskStartedPayload(
                task_id=task_id,
                agent_name=agent_name,
                instruction_preview="(discovered)",
                ts=now,
            )))

    async def _guess_agent_name(self, thread_id: str, run_dict: dict) -> str | None:
        # Try thread metadata first; some workflows stash agent name there.
        try:
            assert self._client is not None
            th = await self._client.threads.get(thread_id=thread_id)
            md = th.get("metadata") if isinstance(th, dict) else getattr(th, "metadata", None)
            if isinstance(md, dict):
                hint = md.get("agent_name") or md.get("assistant_name")
                if hint:
                    return str(hint)
        except Exception:
            pass
        return None

    async def _poll_known_tasks(self) -> None:
        """Check every non-terminal task in the mirror for status changes.

        Crucial: ``interrupt()`` makes ``run.status`` go to ``"success"`` (the
        run reached an interrupt boundary cleanly) while ``thread.status`` goes
        to ``"interrupted"``. We must check both.
        """
        assert self._client is not None
        for task in self._mirror.list_non_terminal():
            if not task.sub_thread_id:
                continue
            # Timeout check.
            if (time.time() - task.started_at) > self._task_timeout:
                await self._enforce_timeout(task)
                continue
            try:
                runs = await self._client.runs.list(thread_id=task.sub_thread_id, limit=1)
            except Exception:
                logger.debug("runs.list failed for %s", task.task_id, exc_info=True)
                continue
            if not runs:
                continue
            run = runs[0] if isinstance(runs[0], dict) else runs[0].__dict__
            run_status = run.get("status") or task.status
            run_id = run.get("run_id")

            # If the latest run is still in flight, we just observe and move on.
            if run_status in _NON_TERMINAL:
                if task.status != run_status:
                    await self._mirror.set_status(task.task_id, run_status)
                continue

            # Run reached a terminal phase. Fetch the thread to disambiguate
            # "real success/error" from "paused at interrupt".
            try:
                thread = await self._client.threads.get(thread_id=task.sub_thread_id)
            except Exception:
                logger.debug("threads.get failed for %s", task.task_id, exc_info=True)
                continue
            thread_status = (
                thread.get("status") if isinstance(thread, dict)
                else getattr(thread, "status", None)
            ) or ""

            if thread_status == "interrupted":
                await self._handle_interrupt(task, run_id, thread)
                continue

            # Otherwise the run is genuinely done — propagate run.status to the
            # mirror as the terminal verdict.
            if run_status in TERMINAL_STATUSES or run_status in ("success", "error"):
                await self._handle_terminal(task, run, run_status)
                continue

            # Unknown — record and re-check next cycle.
            await self._mirror.set_status(task.task_id, run_status)

    # ------------------------------------------------------------------
    # Interrupt handling
    # ------------------------------------------------------------------

    async def _handle_interrupt(
        self,
        task: MirroredTask,
        run_id: str | None,
        thread: Any | None = None,
    ) -> None:
        """Route every fresh ``Interrupt`` to the user and resume the run.

        deepagents/langgraph allow multiple Interrupts in flight; we route
        them sequentially. ``_handled_interrupts`` prevents re-asking the
        same interrupt across slow resume cycles.

        When the caller has already fetched the thread object, pass it in
        to save a second roundtrip.
        """
        assert self._client is not None
        if thread is None:
            try:
                thread = await self._client.threads.get(thread_id=task.sub_thread_id)
            except Exception:
                logger.exception("threads.get failed during interrupt handling")
                return
        interrupts_map: dict[str, list[Any]] = {}
        if isinstance(thread, dict):
            interrupts_map = thread.get("interrupts") or {}
        else:
            interrupts_map = getattr(thread, "interrupts", {}) or {}

        # Flatten and de-dupe by id.
        already = self._handled_interrupts.setdefault(task.task_id, set())
        pending: list[tuple[str, Any]] = []
        for _task_id_in_thread, ilist in (interrupts_map.items() if isinstance(interrupts_map, dict) else []):
            for it in ilist or []:
                it_id = it.get("id") if isinstance(it, dict) else getattr(it, "id", None)
                if not it_id or it_id in already:
                    continue
                value = it.get("value") if isinstance(it, dict) else getattr(it, "value", {})
                pending.append((it_id, value))

        if not pending:
            # Status says interrupted but no fresh interrupts — record state
            # and try again next cycle.
            await self._mirror.set_status(task.task_id, "interrupted")
            return

        for it_id, value in pending:
            ask_id = str(uuid.uuid4())
            ask = self._build_ask(task, ask_id, it_id, value)
            await self._mirror.set_status(
                task.task_id, "awaiting_user", pending_ask_id=ask_id,
            )
            await self._emit(ChannelEvent(payload=AsyncTaskAwaitingUserPayload(
                task_id=task.task_id,
                agent_name=task.agent_name,
                ask_id=ask_id,
                title=ask.title,
                ts=time.time(),
            )))
            try:
                reply = await self._ask(ask)
            except Exception:
                logger.exception("ask_handler raised; auto-rejecting")
                reply = "reject"
            already.add(it_id)
            try:
                await self._client.runs.create(
                    thread_id=task.sub_thread_id,
                    assistant_id=task.graph_id,
                    command={"resume": reply},
                    multitask_strategy="interrupt",
                )
            except Exception:
                logger.exception("runs.create(resume=...) failed for %s", task.task_id)
                await self._mirror.set_status(task.task_id, "error", error="resume_failed")
                await self._emit(ChannelEvent(payload=AsyncTaskCompletedPayload(
                    task_id=task.task_id,
                    agent_name=task.agent_name,
                    ok=False,
                    summary="resume_failed",
                    duration_sec=time.time() - task.started_at,
                    ts=time.time(),
                )))
                return
            await self._mirror.set_status(task.task_id, "running", pending_ask_id=None)
            await self._emit(ChannelEvent(payload=AsyncTaskProgressPayload(
                task_id=task.task_id,
                agent_name=task.agent_name,
                kind_hint="resumed",
                text=f"resumed after user reply ({len(reply)} chars)",
                ts=time.time(),
            )))

    def _build_ask(
        self,
        task: MirroredTask,
        ask_id: str,
        interrupt_id: str,
        value: Any,
    ) -> AskPrompt:
        # Reuse the same shape the orchestrator already passes to ChannelRouter.
        # We delegate title/body formatting to whoever owns ``_title_for_interrupt``
        # in the daemon's existing helpers — but we can't import that without a
        # circular dep on orchestrator_loop, so we replicate the minimal logic.
        if isinstance(value, dict):
            iv: dict[str, Any] = dict(value)
        else:
            iv = {"question": str(value)}
        # Carry interrupt_id so consumers can correlate if they want.
        iv.setdefault("type", "user_question")
        iv["interrupt_id"] = interrupt_id
        iv.setdefault("agent_path", f"{self._agent_path_root}/{task.agent_name}#bg")
        title = _first_str(
            iv.get("title") if isinstance(iv.get("title"), str) else None,
            f"Background task: {task.agent_name}",
        )
        body = _first_str(
            iv.get("body") if isinstance(iv.get("body"), str) else None,
            iv.get("question") if isinstance(iv.get("question"), str) else None,
            f"task_id={task.task_id[:8]}",
        )
        options = iv.get("options") if isinstance(iv.get("options"), list) else ["approve", "reject"]
        return AskPrompt(
            ask_id=ask_id,
            title=title,
            body=body,
            options=options or [],
            interrupt_value=iv,
            session_id=task.parent_thread_id or task.task_id,
            agent_path=iv["agent_path"],
        )

    # ------------------------------------------------------------------
    # Terminal status handling
    # ------------------------------------------------------------------

    async def _handle_terminal(self, task: MirroredTask, run: dict, status: str) -> None:
        assert self._client is not None
        summary = ""
        error_text: str | None = None
        if status == "success":
            try:
                thread = await self._client.threads.get(thread_id=task.sub_thread_id)
                values = thread.get("values") if isinstance(thread, dict) else getattr(thread, "values", None)
                summary = _last_message_text(values)[:400]
            except Exception:
                logger.debug("threads.get for terminal summary failed", exc_info=True)
                summary = "(completed with no readable output)"
        elif status == "error":
            err = run.get("error") if isinstance(run, dict) else getattr(run, "error", None)
            error_text = str(err) if err else "(no error detail)"
            summary = error_text
        else:  # cancelled / timeout
            summary = status

        ok = status == "success"
        duration = time.time() - task.started_at
        await self._mirror.set_status(
            task.task_id, status, summary=summary, error=error_text,
        )
        await self._emit(ChannelEvent(payload=AsyncTaskCompletedPayload(
            task_id=task.task_id,
            agent_name=task.agent_name,
            ok=ok,
            summary=summary[:300],
            duration_sec=duration,
            ts=time.time(),
        )))

    async def _enforce_timeout(self, task: MirroredTask) -> None:
        """Cancel a task that exceeded the wall-clock timeout."""
        assert self._client is not None
        if not task.sub_thread_id:
            return
        try:
            runs = await self._client.runs.list(thread_id=task.sub_thread_id, limit=1)
            for r in runs or []:
                rid = r.get("run_id") if isinstance(r, dict) else getattr(r, "run_id", None)
                if rid:
                    await self._client.runs.cancel(thread_id=task.sub_thread_id, run_id=rid)
        except Exception:
            logger.debug("timeout cancel failed", exc_info=True)
        await self._mirror.set_status(
            task.task_id, "timeout",
            error=f"per_task_timeout ({int(self._task_timeout)}s)",
            summary="task exceeded wall-clock timeout",
        )
        await self._emit(ChannelEvent(payload=AsyncTaskCompletedPayload(
            task_id=task.task_id,
            agent_name=task.agent_name,
            ok=False,
            summary=f"timeout after {int(self._task_timeout)}s",
            duration_sec=time.time() - task.started_at,
            ts=time.time(),
        )))
