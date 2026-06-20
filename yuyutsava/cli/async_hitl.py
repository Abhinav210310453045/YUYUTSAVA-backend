"""CLI HITL bridge for Mode 1 (standalone CLI deepagent, no daemon).

When the CLI hosts its own ``AsyncSubagentHost``, the ``AsyncTaskHealthWatcher``
needs the same two callables the daemon's ``ChannelRouter`` provides:

  * an ``ask_handler(AskPrompt) -> str`` that blocks until the user replies, and
  * an ``event_sink(ChannelEvent) -> None`` that records progress for display.

The daemon path lives in ``ChannelRouter``. For the CLI we provide a thin
``CliHitlBridge`` that wraps stdin/stdout. Background-task asks are printed
inline; progress events are queued for the REPL to drain between user turns.

Why between turns? The user is typically composing input when bg events
arrive — flushing them mid-keystroke would corrupt their line. ``drain()``
returns the queue and the REPL prints them just before redrawing the prompt.
Asks are urgent enough to flush immediately (they block on stdin anyway).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from collections import deque
from typing import Any, Deque

from yuyutsava.daemon.channels import (
    AskPrompt,
    AsyncTaskAwaitingUserPayload,
    AsyncTaskCompletedPayload,
    AsyncTaskProgressPayload,
    AsyncTaskStartedPayload,
    ChannelEvent,
)


logger = logging.getLogger("yuyutsava.cli.async_hitl")


class CliRemoteHitl:
    """Daemon-deferring HITL for the chat REPL (the preferred path).

    When a daemon is running it owns the async host and the single, idempotent
    decision pipeline. This client just *observes* the daemon's SSE stream and
    *submits* answers over REST — it never resolves interrupts locally. That
    means a background-task approval can be answered from the CLI **or** the UI
    and stays in sync (whichever answers first wins; the other surface clears on
    the ``ask_resolved`` event), and the CLI prompt never blocks/freezes.

    Notices are printed to **stdout** (not stderr): while prompt_toolkit owns the
    terminal it wraps stdout via ``patch_stdout`` and renders these cleanly above
    the live prompt — no raw-ANSI leak. Notices are plain text for robustness.
    """

    def __init__(self, client: Any, *, out=None) -> None:
        self._client = client            # CliAttachClient (stream + respond_ask)
        self._out = out if out is not None else sys.stdout
        self._asks: dict[str, dict] = {}
        self._proposals: dict[str, dict] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="cli-remote-hitl")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        with contextlib.suppress(Exception):
            await self._client.close()

    # -- stream consumer -------------------------------------------------

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async for frame in self._client.stream():
                    if self._stop.is_set():
                        break
                    self._on_frame(frame)
                    backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._stop.is_set():
                    break
                logger.debug("cli remote hitl stream error; retrying", exc_info=True)
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    def _on_frame(self, frame: Any) -> None:
        ev = getattr(frame, "event", "")
        d = getattr(frame, "data", None) or {}
        if ev == "ask":
            a = d.get("ask") or {}
            if a.get("ask_id"):
                self._asks[a["ask_id"]] = a
                self._render_ask(a)
        elif ev == "ask_resolved":
            aid = d.get("ask_id")
            if aid and self._asks.pop(aid, None) is not None:
                self._print(f"[bg] approval {aid[:8]} resolved")
        elif ev == "proposal":
            p = d.get("proposal") or {}
            pid = p.get("proposal_id")
            if pid and pid not in self._proposals:
                self._proposals[pid] = p
                summ = (p.get("summary") or "").strip().replace("\n", " ")[:80]
                self._print(f"[proposal {pid[:8]}] {summ}  — approve in the UI")
        elif ev == "proposal_resolved":
            self._proposals.pop(d.get("proposal_id"), None)
        # "event"/"hello" frames are ignored: the REPL renders its own turn stream.

    def _render_ask(self, a: dict) -> None:
        from yuyutsava.consent import is_permission_ask

        title = a.get("title") or "Permission request"
        agent = a.get("agent_path") or ""
        tag = " [background]" if agent.endswith("#bg") else ""
        aid8 = (a.get("ask_id") or "")[:8]
        pending = len(self._asks)
        count = f"  ({pending} pending)" if pending > 1 else ""
        self._print("")
        self._print(f"▣ {title}{tag}{count}")
        for ln in (a.get("body") or "").strip().splitlines():
            self._print(f"  {ln}")
        options = a.get("options") or []
        if is_permission_ask(options):
            # Bare words resolve the active (oldest) ask — no id to type. Scope
            # words appear only when the daemon offered them (low/medium risk).
            scope = ""
            if "session" in options or "project" in options:
                scope = " / [s]ession / [p]roject"
            self._print(f"  -> [y]es / [n]o{scope}   (or /approve {aid8} · or answer in the UI)")
        elif options:
            self._print(f"  -> /approve {aid8}  |  /reject {aid8}   (or answer in the UI)")
        else:
            self._print(f"  -> /reply {aid8} <text>   (or answer in the UI)")

    # -- REPL queries / answers -----------------------------------------

    def list_pending(self) -> list[dict]:
        return list(self._asks.values())

    def active(self) -> dict | None:
        """The head (oldest) pending ask — the one a bare y/n answers."""
        for a in self._asks.values():
            return a
        return None

    def _find(self, prefix: str) -> dict | None:
        prefix = (prefix or "").strip()
        if not prefix:
            # No id given → the active (oldest) ask, so bare y/n needs no id.
            return self.active()
        for aid, a in self._asks.items():
            if aid == prefix or aid.startswith(prefix):
                return a
        return None

    async def answer(self, prefix: str, response: str) -> str:
        a = self._find(prefix)
        if a is None:
            if not (prefix or "").strip():
                return "no pending approvals"
            return f"no pending approval matches {prefix!r} (try /asks)"
        ok = await self._client.respond_ask(a["ask_id"], response)
        if ok:
            self._asks.pop(a["ask_id"], None)   # optimistic; ask_resolved confirms
            return f"sent '{response}' for {a['ask_id'][:8]}"
        return f"failed to send response for {a['ask_id'][:8]}"

    def _print(self, line: str) -> None:
        with contextlib.suppress(Exception):
            print(line, file=self._out, flush=True)


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"


class CliHitlBridge:
    """Stdin/stdout bridge for Mode 1 async-subagent HITL.

    Thread-safe with respect to the REPL: ``post_event`` is called from the
    watcher's asyncio task, ``drain()`` is called from the REPL between turns.
    Both share an ``asyncio.Lock`` over the queue.
    """

    def __init__(self) -> None:
        self._queue: Deque[ChannelEvent] = deque()
        self._lock = asyncio.Lock()
        self._started: dict[str, float] = {}   # task_id -> start ts (for elapsed)

    # ------------------------------------------------------------------
    # Watcher → bridge
    # ------------------------------------------------------------------

    async def post_event(self, ev: ChannelEvent) -> None:
        # Track start times so progress lines can show elapsed.
        p = ev.payload
        if isinstance(p, AsyncTaskStartedPayload):
            self._started[p.task_id] = p.ts or time.time()
        async with self._lock:
            self._queue.append(ev)

    async def post_ask(self, ask: AskPrompt) -> str:
        """Block on stdin for a single ask.

        Asks for background subagents flush queued events first so context
        about which task is asking is visible right above the prompt.
        """
        from yuyutsava.consent import decision_token, is_permission_ask

        # Flush queued events into stderr so the user sees recent context.
        await self._flush_to_stderr()
        title = ask.title or "Background task question"
        body = ask.body or ""
        agent_path = ask.agent_path or ""
        options = ask.options or []
        is_permission = is_permission_ask(options)

        prefix = f"\n\033[36m▣ {title}\033[0m"
        if agent_path:
            prefix += f"  \033[2m({agent_path})\033[0m"
        print(prefix, file=sys.stderr, flush=True)
        if body:
            print(f"  {body}", file=sys.stderr, flush=True)
        if is_permission:
            scope = " / [s]ession / [p]roject" if ("session" in options or "project" in options) else ""
            print(f"  \033[2m[y]es / [n]o{scope}\033[0m", file=sys.stderr, flush=True)
        elif options:
            print(f"  options: {' / '.join(options)}", file=sys.stderr, flush=True)

        prompt = "approve/reject> " if is_permission else "> "
        try:
            line = await asyncio.get_running_loop().run_in_executor(
                None, lambda: input(prompt).strip()
            )
        except (EOFError, KeyboardInterrupt):
            return "reject"
        if is_permission:
            return decision_token(line) or "reject"
        return line or "reject"

    # ------------------------------------------------------------------
    # REPL → bridge
    # ------------------------------------------------------------------

    async def drain(self) -> list[ChannelEvent]:
        """Pop all queued events. Called by the REPL between turns."""
        async with self._lock:
            out = list(self._queue)
            self._queue.clear()
        return out

    async def render_between_turns(self) -> None:
        """Convenience: drain + print each event as a colored stderr banner."""
        evs = await self.drain()
        for ev in evs:
            self._print_event(ev)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _flush_to_stderr(self) -> None:
        async with self._lock:
            evs = list(self._queue)
            self._queue.clear()
        for ev in evs:
            self._print_event(ev)

    def _print_event(self, ev: ChannelEvent) -> None:
        p = ev.payload
        if isinstance(p, AsyncTaskStartedPayload):
            line = f"\033[36m[bg started]\033[0m {p.agent_name}  task={p.task_id[:8]}  {p.instruction_preview}"
        elif isinstance(p, AsyncTaskProgressPayload):
            line = f"\033[2m[bg progress]\033[0m {p.agent_name}  task={p.task_id[:8]}  {p.kind_hint}: {p.text}"
        elif isinstance(p, AsyncTaskAwaitingUserPayload):
            line = f"\033[33m[bg awaiting user]\033[0m {p.agent_name}  task={p.task_id[:8]}  ask={p.ask_id[:8]}  {p.title}"
        elif isinstance(p, AsyncTaskCompletedPayload):
            colour = "\033[32m" if p.ok else "\033[31m"
            elapsed = _fmt_elapsed(p.duration_sec)
            summary = (p.summary or "").replace("\n", " ")[:100]
            line = f"{colour}[bg done {'OK' if p.ok else 'FAIL'}]\033[0m {p.agent_name}  task={p.task_id[:8]}  elapsed={elapsed}  {summary}"
        else:
            # Ignore non-async-task events (token/log/etc.) — the REPL has its
            # own streaming display for those.
            return
        print(line, file=sys.stderr, flush=True)
