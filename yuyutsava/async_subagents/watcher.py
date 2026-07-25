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
import json
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
from yuyutsava.async_subagents.launch_index import LaunchIndex
from yuyutsava.async_subagents.interrupt_middleware import (
    build_synthetic_toolmessages,
    find_pending_tool_calls,
)
from yuyutsava.async_subagents.mirror import (
    AsyncTaskMirror,
    MirroredTask,
    TERMINAL_STATUSES,
)
from yuyutsava.daemon.interrupt_format import (
    body_for_interrupt,
    options_for_interrupt,
    title_for_interrupt,
)

logger = logging.getLogger("yuyutsava.async_subagents.watcher")


AskHandler = Callable[[AskPrompt], Awaitable[str]]
EventSink = Callable[[ChannelEvent], Awaitable[None]]
# Called once per task when it reaches a terminal status, so the daemon can wake
# the master agent on the originating thread. (task, ok, summary) -> None.
CompletionSink = Callable[[MirroredTask, bool, str], Awaitable[None]]


# Statuses we treat as "still running" for HITL/polling purposes.
_NON_TERMINAL = frozenset({"pending", "running"})


def _first_str(*parts: str | None) -> str:
    for p in parts:
        if p:
            return p
    return ""


def _clean_agent_name(raw: str | None) -> str | None:
    """Display name from a graph/assistant id (strips the async ``-bg`` suffix)."""
    if not raw or not isinstance(raw, str):
        return None
    name = raw.strip()
    if name.endswith("-bg"):
        name = name[:-3]
    return name or None


def _extract_thread_id(thread_obj: Any) -> str | None:
    if isinstance(thread_obj, dict):
        return thread_obj.get("thread_id")
    return getattr(thread_obj, "thread_id", None)


def _first_run_status(runs: Any) -> str:
    """Status of the latest (newest-first) run, or '' when there are none."""
    if not runs:
        return ""
    r = runs[0]
    if isinstance(r, dict):
        return r.get("status") or ""
    return getattr(r, "status", "") or ""


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


def _content_text(content: Any) -> str:
    """Flatten a message ``content`` (str | list-of-blocks) to plain text."""
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


_COMPACT_ERROR_CAP = 300


def compact_error(raw: str | None) -> str:
    """Reduce a possibly-huge error/traceback to a short, single-line reason.

    Background-task errors get injected into the launching agent's context, so a
    multi-line traceback would bloat it. We keep only the most meaningful line —
    the final ``Type: message`` of a Python traceback, else the last non-empty
    line — and hard-cap the length. The full text stays available via the
    per-task logs endpoint.
    """
    if not raw:
        return "(no error detail)"
    text = str(raw).strip()
    if not text:
        return "(no error detail)"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    chosen = text
    if lines:
        # A Python traceback ends with the exception line ("ValueError: ...").
        # That is the most useful single line; fall back to the last line.
        for ln in reversed(lines):
            if not ln.startswith(("File \"", "Traceback", "  ", "during", "During")):
                chosen = ln
                break
        else:
            chosen = lines[-1]
    chosen = " ".join(chosen.split())
    if len(chosen) > _COMPACT_ERROR_CAP:
        chosen = chosen[: _COMPACT_ERROR_CAP - 1].rstrip() + "…"
    return chosen


def _run_error_text(run: Any) -> str:
    """Extract an error string from a run object across SDK shapes.

    langgraph-api serializes a run failure as
    ``{"error": <ExceptionType>, "message": <detail>}`` and masks the message to
    ``"An internal error occurred"`` for any exception type outside its allow-list
    (serde.py). The exception *type* survives that masking and is often the only
    diagnostic clue — ``RateLimitError`` vs ``TimeoutError`` vs a real bug — so we
    surface both type and message rather than the message alone.
    """
    if isinstance(run, dict):
        err = run.get("error")
    else:
        err = getattr(run, "error", None)
    if not err:
        return ""
    if isinstance(err, dict):
        etype = str(err.get("error") or "").strip()
        msg = str(err.get("message") or "").strip()
        if etype and msg:
            return f"{etype}: {msg}"
        return etype or msg
    return str(err).strip()


def _tool_message_is_error(status: Any, content: Any) -> bool:
    """Whether a ToolMessage genuinely represents a failure.

    The message-level ``status == "error"`` is authoritative. Otherwise, if the
    content is a JSON tool envelope, trust its own ``status``/``error`` fields: a
    successful ``{"status": "success", "error": null}`` result must NOT count as
    an error merely because the literal word ``"error"`` appears as a key. Only
    non-JSON content falls back to a traceback/exception keyword sniff.
    """
    if status == "error":
        return True
    text = _content_text(content).strip()
    if not text:
        return False
    try:
        envelope = json.loads(text)
    except (ValueError, TypeError):
        envelope = None
    if isinstance(envelope, dict):
        if str(envelope.get("status", "")).lower() == "error":
            return True
        if envelope.get("error"):
            return True
        # A well-formed envelope that declares its own status/error is trusted:
        # having reached here it did not fail, so it is a success, not an error.
        if "status" in envelope or "error" in envelope:
            return False
    low = text.lower()
    return any(kw in low for kw in ("traceback (most recent call last)", "exception:", "error:"))


def _error_text_from_thread(values: Any) -> str:
    """Best-effort error reason from a failed sub-thread's messages.

    Used when the run object carries no ``error`` field. Returns the most recent
    ToolMessage that *genuinely* failed, or "" when none is found — so the caller
    can substitute a clear generic reason instead of mislabeling a benign success
    payload or completion summary as the error.
    """
    if not isinstance(values, dict):
        return ""
    msgs = values.get("messages") or []
    for m in reversed(msgs):
        mtype = m.get("type") or m.get("role") if isinstance(m, dict) else getattr(m, "type", "")
        status = m.get("status") if isinstance(m, dict) else getattr(m, "status", None)
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if mtype == "tool" and _tool_message_is_error(status, content):
            txt = _content_text(content).strip()
            if txt:
                return txt
    return ""


def _transcript_rows(values: Any) -> list[dict]:
    """Flatten a sub-thread's ``messages`` into UI-friendly transcript rows.

    Each row is ``{role, text, tool_name?, tool_args?, status?}``. Tool calls
    surface their name + args; tool results and assistant/human text surface
    their flattened content. Shares ``_content_text`` with the live progress
    stream so the on-demand transcript and the streamed steps agree.
    """
    if not isinstance(values, dict):
        return []
    rows: list[dict] = []
    for m in values.get("messages") or []:
        if isinstance(m, dict):
            mtype = m.get("type") or m.get("role") or ""
            content = m.get("content", "")
            tcs = m.get("tool_calls")
            status = m.get("status")
            name = m.get("name")
        else:
            mtype = getattr(m, "type", "") or ""
            content = getattr(m, "content", "")
            tcs = getattr(m, "tool_calls", None)
            status = getattr(m, "status", None)
            name = getattr(m, "name", None)
        role = "assistant" if mtype in ("ai", "assistant") else (
            "user" if mtype in ("human", "user") else (mtype or "message")
        )
        text = _content_text(content).strip()
        if text:
            rows.append({"role": role, "text": text})
        for tc in tcs or []:
            tc_name = tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
            tc_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            try:
                args_str = json.dumps(tc_args, ensure_ascii=False) if tc_args else ""
            except (TypeError, ValueError):
                args_str = str(tc_args)
            rows.append({"role": "tool_call", "tool_name": tc_name, "tool_args": args_str, "text": ""})
        if mtype == "tool" and text:
            rows[-1]["role"] = "tool_result"
            if name:
                rows[-1]["tool_name"] = name
            if status:
                rows[-1]["status"] = status
    return rows


def _new_progress_steps(values: Any, seen: int) -> tuple[list[tuple[str, str]], int]:
    """Diff a sub-thread's message history into new progress steps.

    Returns ``(steps, total)`` where ``steps`` is a list of
    ``(kind_hint, text)`` for each message past ``seen`` — a tool call
    (``"tool_call"``, ``"<name> <args>"``) or a chunk of assistant text
    (``"text"``). Tool results and human turns are skipped to keep the live
    stream readable; the full payload is always available via the event's copy
    button on the UI side. ``total`` is the new high-water message count.
    """
    if not isinstance(values, dict):
        return [], seen
    msgs = values.get("messages") or []
    total = len(msgs)
    if total <= seen:
        return [], total
    steps: list[tuple[str, str]] = []
    for m in msgs[seen:]:
        if isinstance(m, dict):
            tcs = m.get("tool_calls")
            content = m.get("content", "")
            mtype = m.get("type") or m.get("role") or ""
        else:
            tcs = getattr(m, "tool_calls", None)
            content = getattr(m, "content", "")
            mtype = getattr(m, "type", "") or ""
        if tcs:
            for tc in tcs:
                name = tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
                args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                try:
                    args_str = json.dumps(args, ensure_ascii=False) if args else ""
                except (TypeError, ValueError):
                    args_str = str(args)
                steps.append(("tool_call", f"{name} {args_str}".strip()[:200]))
        elif mtype in ("ai", "assistant"):
            text = _content_text(content).strip()
            if text:
                steps.append(("text", text[:200]))
    return steps, total


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
        completion_sink: CompletionSink | None = None,
        launch_index: LaunchIndex | None = None,
    ) -> None:
        self._mirror = mirror
        self._host_url = host_url
        self._ask = ask_handler
        self._emit = event_sink
        self._agent_path_root = agent_path_root
        self._poll = poll_interval_sec
        self._task_timeout = per_task_timeout_sec
        self._headers = headers
        # Wakes the master agent when a bg task finishes (daemon path). When
        # None (e.g. CLI Mode-1) the watcher only emits the UI completion event.
        self._completion_sink = completion_sink
        # Links a discovered sub-thread back to the launching turn/channel.
        self._launch_index = launch_index
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._client = None
        self._known_threads: set[str] = set()
        # task_id -> set of interrupt_ids already routed (avoid double-ask on slow resume)
        self._handled_interrupts: dict[str, set[str]] = {}
        # task_id -> count of sub-thread messages already streamed as progress
        # (so each cycle only emits the *new* steps). Cleared on terminal.
        self._progress_seen: dict[str, int] = {}
        # sub_thread_ids we've already healed once (orphaned tool_call repair),
        # so a persistently-failing thread isn't patched on every poll cycle.
        self._healed_threads: set[str] = set()

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
        """For a freshly-discovered thread, mirror only its *still-running* runs.

        Threads whose latest run is already terminal are tasks from a previous
        session that have finished — we mark them known (done in the caller) but
        do NOT re-announce them as ``[bg started] (discovered)`` on every boot.
        Only genuinely in-flight tasks are surfaced/tracked, so the orchestrator
        and CLI still know about background work that is actually still running.
        """
        assert self._client is not None
        try:
            runs = await self._client.runs.list(thread_id=thread_id, limit=5)
        except Exception:
            return
        # The latest run (newest-first) decides whether this thread is live.
        latest = _first_run_status(runs)
        if latest not in _NON_TERMINAL:
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
            # We don't know the agent_name from runs.list alone; try thread
            # metadata, then fall back to the run's assistant/graph id (e.g.
            # "general-purpose" / "file-organizer") instead of an opaque label.
            agent_name = (
                await self._guess_agent_name(thread_id, run_dict)
                or _clean_agent_name(run_dict.get("assistant_id"))
                or "background task"
            )
            now = time.time()
            # Link back to the launching conversation when the orchestrator
            # recorded it (improves bg-ask routing + lets us wake the master on
            # the original thread at completion).
            rec = self._launch_index.get(task_id) if self._launch_index else None
            await self._mirror.upsert(MirroredTask(
                task_id=task_id,
                agent_name=agent_name,
                graph_id=run_dict.get("assistant_id") or "",
                instruction="(discovered)",
                status=status,
                started_at=now,
                last_update_at=now,
                sub_thread_id=thread_id,
                parent_thread_id=rec.parent_thread_id if rec else None,
                origin=rec.origin if rec else None,
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

            # If the latest run is still in flight, observe status + stream any
            # new subagent steps so the task reports progress instead of going
            # dark between start and completion.
            if run_status in _NON_TERMINAL:
                if task.status != run_status:
                    await self._mirror.set_status(task.task_id, run_status)
                await self._emit_progress(task)
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

            # Self-heal: a run that errored on an orphaned tool_call leaves the
            # thread wedged (every later read/op fails _validate_chat_history).
            # Patch it once so the thread is valid for the logs endpoint and any
            # future operation, then report the (now compact) error normally.
            if run_status == "error" and task.sub_thread_id not in self._healed_threads:
                await self._heal_orphaned_tool_calls(task, thread)

            # Otherwise the run is genuinely done — propagate run.status to the
            # mirror as the terminal verdict.
            if run_status in TERMINAL_STATUSES or run_status in ("success", "error"):
                await self._handle_terminal(task, run, run_status)
                continue

            # Unknown — record and re-check next cycle.
            await self._mirror.set_status(task.task_id, run_status)

    async def _emit_progress(self, task: MirroredTask) -> None:
        """Stream any new subagent steps as ``AsyncTaskProgressPayload`` events.

        Polls the sub-thread's message history and emits one progress event per
        new tool call / assistant-text step since the last cycle, so background
        subagents report step-by-step through the same channel the foreground
        task uses. Best-effort: failures are logged and skipped.
        """
        assert self._client is not None
        if not task.sub_thread_id:
            return
        try:
            thread = await self._client.threads.get(thread_id=task.sub_thread_id)
        except Exception:
            logger.debug("threads.get for progress failed for %s", task.task_id, exc_info=True)
            return
        values = (
            thread.get("values") if isinstance(thread, dict)
            else getattr(thread, "values", None)
        )
        seen = self._progress_seen.get(task.task_id, 0)
        steps, total = _new_progress_steps(values, seen)
        if total == seen:
            return
        self._progress_seen[task.task_id] = total
        for kind_hint, text in steps:
            await self._emit(ChannelEvent(payload=AsyncTaskProgressPayload(
                task_id=task.task_id,
                agent_name=task.agent_name,
                kind_hint=kind_hint,
                text=text,
                ts=time.time(),
            )))

    # ------------------------------------------------------------------
    # Transcript (for the UI logs endpoint)
    # ------------------------------------------------------------------

    async def get_task_transcript(self, task_id: str) -> list[dict] | None:
        """Return a background task's full message transcript, or None.

        Looks up the task in the mirror, fetches its sub-thread state from the
        langgraph host, and flattens ``messages`` into UI-friendly rows. Returns
        ``None`` when the task is unknown; ``[]`` when it has no messages yet.
        """
        if self._client is None:
            return None
        task = self._mirror.get(task_id)
        if task is None or not task.sub_thread_id:
            return None
        try:
            thread = await self._client.threads.get(thread_id=task.sub_thread_id)
        except Exception:
            logger.debug("get_task_transcript threads.get failed for %s", task_id, exc_info=True)
            return []
        values = (
            thread.get("values") if isinstance(thread, dict)
            else getattr(thread, "values", None)
        )
        return _transcript_rows(values)

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

        # Ask the user for each pending interrupt, then resume ONCE with a map
        # keyed by interrupt id. LangGraph requires the keyed form whenever more
        # than one interrupt is pending (a bare ``{"resume": value}`` raises
        # "you must specify the interrupt id when resuming").
        replies: dict[str, str] = {}
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
            replies[it_id] = reply

        try:
            await self._client.runs.create(
                thread_id=task.sub_thread_id,
                assistant_id=task.graph_id,
                command={"resume": replies},
                multitask_strategy="interrupt",
            )
        except Exception:
            logger.exception("runs.create(resume=...) failed for %s", task.task_id)
            updated = await self._mirror.set_status(task.task_id, "error", error="resume_failed")
            await self._emit(ChannelEvent(payload=AsyncTaskCompletedPayload(
                task_id=task.task_id,
                agent_name=task.agent_name,
                ok=False,
                summary="resume_failed",
                duration_sec=time.time() - task.started_at,
                ts=time.time(),
            )))
            await self._notify_complete(updated or task, False, "resume_failed")
            return
        await self._mirror.set_status(task.task_id, "running", pending_ask_id=None)
        await self._emit(ChannelEvent(payload=AsyncTaskProgressPayload(
            task_id=task.task_id,
            agent_name=task.agent_name,
            kind_hint="resumed",
            text=f"resumed after {len(replies)} user repl{'y' if len(replies) == 1 else 'ies'}",
            ts=time.time(),
        )))

    async def resume_interrupt(
        self, task_id: str, interrupt_id: str | None, reply: str
    ) -> bool:
        """Push a reply into a task parked on an interrupt. Returns success.

        The restart path: an ask raised before the daemon went down has no
        in-memory waiter left, but the run itself is still checkpointed and
        interrupted on the subagent host. Answering it means re-entering the
        run directly rather than waking a future that no longer exists.
        """
        task = self._mirror.get(task_id)
        if task is None:
            logger.warning("resume_interrupt: unknown task %s", task_id)
            return False
        if not interrupt_id:
            logger.warning(
                "resume_interrupt: task %s has no interrupt id — cannot route "
                "the reply", task_id,
            )
            return False
        try:
            await self._client.runs.create(
                thread_id=task.sub_thread_id,
                assistant_id=task.graph_id,
                command={"resume": {interrupt_id: reply}},
                multitask_strategy="interrupt",
            )
        except Exception:  # noqa: BLE001
            logger.exception("resume_interrupt: runs.create failed for %s", task_id)
            return False
        await self._mirror.set_status(task_id, "running", pending_ask_id=None)
        logger.info("resume_interrupt: task %s resumed with a stored reply", task_id)
        return True

    def _build_ask(
        self,
        task: MirroredTask,
        ask_id: str,
        interrupt_id: str,
        value: Any,
    ) -> AskPrompt:
        # Format identically to foreground asks via the shared formatter, so the
        # CLI prompt and the UI AskCard get a clean title/body (no raw-JSON blob).
        # Preserve the interrupt's real ``type`` (task_runner_permission /
        # permission_request / user_question) so the formatter dispatches right —
        # only wrap as a free-text question when the value isn't a dict.
        if isinstance(value, dict):
            iv: dict[str, Any] = dict(value)
        else:
            iv = {"type": "user_question", "question": str(value)}
        # Carry interrupt_id so consumers can correlate if they want.
        iv["interrupt_id"] = interrupt_id
        iv.setdefault("agent_path", f"{self._agent_path_root}/{task.agent_name}#bg")
        return AskPrompt(
            ask_id=ask_id,
            title=title_for_interrupt(iv),
            body=body_for_interrupt(iv),
            options=options_for_interrupt(iv),
            interrupt_value=iv,
            session_id=task.parent_thread_id or task.task_id,
            agent_path=iv["agent_path"],
            # A background ask belongs to the task, not to whichever chat
            # happened to launch it: it renders in the Inbox (and the overlay),
            # never inline inside that conversation. task_id + interrupt_id are
            # what let the resume path re-enter the right run afterwards.
            surface="background",
            thread_id=task.parent_thread_id,
            task_id=task.task_id,
            agent_label=task.agent_name,
            interrupt_id=interrupt_id,
        )

    # ------------------------------------------------------------------
    # Terminal status handling
    # ------------------------------------------------------------------

    async def _heal_orphaned_tool_calls(self, task: MirroredTask, thread: Any) -> None:
        """Inject synthetic ToolMessages for any unresolved tool_calls.

        Defensive net for the case the master-side interrupt patch missed: an
        errored thread carrying an ``AIMessage(tool_calls)`` with no matching
        ``ToolMessage`` is otherwise unreadable. Done at most once per thread.
        """
        assert self._client is not None
        if not task.sub_thread_id:
            return
        values = (
            thread.get("values") if isinstance(thread, dict)
            else getattr(thread, "values", None)
        ) or {}
        messages = values.get("messages") if isinstance(values, dict) else None
        pending = find_pending_tool_calls(messages or [])
        if not pending:
            return
        try:
            await self._client.threads.update_state(
                thread_id=task.sub_thread_id,
                values={"messages": build_synthetic_toolmessages(pending)},
            )
            self._healed_threads.add(task.sub_thread_id)
            logger.info(
                "watcher self-heal: patched %d orphaned tool_call(s) on thread %s",
                len(pending), task.sub_thread_id,
            )
        except Exception:
            logger.debug("watcher self-heal update_state failed", exc_info=True)

    async def _fetch_run_error(self, task: MirroredTask, run: Any) -> str:
        """Pull the error detail from the full run record, if the SDK exposes it.

        The run object from ``runs.list`` frequently omits ``error``; fetching the
        run by id often returns the populated field. Best-effort and defensive:
        any failure (or an SDK that never surfaces it) just yields "".
        """
        assert self._client is not None
        run_id = run.get("run_id") if isinstance(run, dict) else getattr(run, "run_id", None)
        if not (task.sub_thread_id and run_id):
            return ""
        try:
            full = await self._client.runs.get(thread_id=task.sub_thread_id, run_id=run_id)
        except Exception:
            logger.debug("runs.get for terminal error failed for %s", task.task_id, exc_info=True)
            return ""
        return _run_error_text(full)

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
            raw = _run_error_text(run)
            if not raw:
                # The list-view run object often omits the error detail; the full
                # run record usually carries it (e.g. langgraph-api's generic
                # "An internal error occurred").
                raw = await self._fetch_run_error(task, run)
            if not raw:
                # Still nothing — recover a genuine tool failure from the thread.
                try:
                    thread = await self._client.threads.get(thread_id=task.sub_thread_id)
                    values = thread.get("values") if isinstance(thread, dict) else getattr(thread, "values", None)
                    raw = _error_text_from_thread(values)
                except Exception:
                    logger.debug("threads.get for terminal error failed", exc_info=True)
            if not raw:
                # No detail anywhere: say so plainly rather than surfacing an
                # unrelated success payload or completion summary as the "error".
                raw = (
                    "run ended in error status with no error detail "
                    "(likely an internal langgraph-api run failure); see task logs"
                )
            # Compact so it never bloats the launching agent's context; the full
            # text remains available via GET /tasks/{id}/logs.
            error_text = compact_error(raw)
            summary = error_text
        else:  # cancelled / timeout
            summary = status

        ok = status == "success"
        duration = time.time() - task.started_at
        self._progress_seen.pop(task.task_id, None)
        updated = await self._mirror.set_status(
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
        await self._notify_complete(updated or task, ok, summary)

    async def _notify_complete(self, task: MirroredTask, ok: bool, summary: str) -> None:
        """Hand a finished task to the completion sink, exactly once.

        A task reaches a terminal status only once (it then drops out of
        ``list_non_terminal``), so this is naturally called once per task; the
        ``notified`` re-check is defensive. The sink (bootstrap) decides whether
        a master wake-up is possible and flips ``notified`` when it enqueues one.
        """
        if self._completion_sink is None:
            return
        cur = self._mirror.get(task.task_id) or task
        if cur.notified:
            return
        try:
            await self._completion_sink(cur, ok, summary)
        except Exception:
            logger.exception("completion_sink failed for %s", task.task_id)

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
        self._progress_seen.pop(task.task_id, None)
        updated = await self._mirror.set_status(
            task.task_id, "timeout",
            error=f"per_task_timeout ({int(self._task_timeout)}s)",
            summary="task exceeded wall-clock timeout",
        )
        summary = f"timeout after {int(self._task_timeout)}s"
        await self._emit(ChannelEvent(payload=AsyncTaskCompletedPayload(
            task_id=task.task_id,
            agent_name=task.agent_name,
            ok=False,
            summary=summary,
            duration_sec=time.time() - task.started_at,
            ts=time.time(),
        )))
        await self._notify_complete(updated or task, False, summary)
