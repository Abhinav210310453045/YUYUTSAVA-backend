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
import sys
import time
from collections import deque
from typing import Deque

from yuyutsava.daemon.channels import (
    AskPrompt,
    AsyncTaskAwaitingUserPayload,
    AsyncTaskCompletedPayload,
    AsyncTaskProgressPayload,
    AsyncTaskStartedPayload,
    ChannelEvent,
)


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
        from yuyutsava.core.streaming import _normalize_yes_no

        # Flush queued events into stderr so the user sees recent context.
        await self._flush_to_stderr()
        title = ask.title or "Background task question"
        body = ask.body or ""
        agent_path = ask.agent_path or ""
        is_permission = set(ask.options or []) == {"approve", "reject"}

        prefix = f"\n\033[36m▣ {title}\033[0m"
        if agent_path:
            prefix += f"  \033[2m({agent_path})\033[0m"
        print(prefix, file=sys.stderr, flush=True)
        if body:
            print(f"  {body}", file=sys.stderr, flush=True)
        if ask.options:
            opt_str = " / ".join(ask.options)
            print(f"  options: {opt_str}", file=sys.stderr, flush=True)
        if is_permission:
            print("  \033[2m[y]es / [n]o  (also: approve / reject)\033[0m", file=sys.stderr, flush=True)

        prompt = "approve/reject> " if is_permission else "> "
        try:
            line = await asyncio.get_running_loop().run_in_executor(
                None, lambda: input(prompt).strip()
            )
        except (EOFError, KeyboardInterrupt):
            return "reject"
        if is_permission:
            return _normalize_yes_no(line)
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
