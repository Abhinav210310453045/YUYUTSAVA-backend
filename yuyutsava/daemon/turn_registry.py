"""Daemon-owned conversation turns, addressed by ``thread_id``.

A conversation turn used to be an :class:`asyncio.Task` held in the WebSocket
handler's local scope: only that one connection could see it, and the handler's
``finally`` cancelled it. Closing a tinker pane, switching TODO cards or
reloading the renderer therefore killed the agent mid-node — while *background*
agents, owned by a daemon-lifetime task and addressed by a persisted id, were
never affected by a UI event at all.

This module gives conversations that same ownership model. A :class:`TurnRun`
is created **by the daemon** and addressed by its ``thread_id``; sockets attach
as *viewers* and detach on disconnect without touching the run. Explicit
cancellation (the Stop button / ``interrupt`` frame /
``POST /conversations/{thread_id}/cancel``) stays the only way to kill a turn.

Modelled directly on the per-task replay ring already in
``daemon/web/services/stream_service.py`` (``TASK_RING_SIZE`` /
``MAX_TRACKED_TASKS`` / ``task_events``): every frame gets a monotonic ``seq``,
the last :data:`TURN_RING_SIZE` are kept per thread, and a reconnecting client
replays the gap by passing ``since_seq`` before it resumes the live stream.

The unit of subscription is the **thread**, not the run — a viewer attaches
once and keeps receiving frames across turn boundaries, which is what lets
``seq`` stay monotonic for the whole conversation and makes ``since_seq``
resumption exact.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("yuyutsava.daemon.turn_registry")


# Per-thread replay ring: enough to refill a client that reconnects mid-turn
# without persisting the firehose (mirrors TASK_RING_SIZE).
TURN_RING_SIZE = 500
# Channels are dropped oldest-idle-first past this bound so an immortal daemon
# can't accumulate rings forever (mirrors MAX_TRACKED_TASKS).
MAX_TRACKED_THREADS = 64
# How long a finished run's channel is kept before sweeping, so a client that
# reconnects just *after* completion still receives its `turn_end`.
FINISHED_RETENTION_SEC = 300.0

# Frame types that are fanned out live but never stored in the ring: streamed
# TTS PCM is megabytes per turn, and the persisted WAV (`_persist_voice_message`
# → `audio_url`) is already the replay path for spoken replies.
EPHEMERAL_TYPES = frozenset({"audio_chunk"})
# …and a viewer whose socket is this far behind on audio stops being sent more
# of it. Prose is never dropped; real-time audio that late is already stale.
EPHEMERAL_BACKLOG = 48

# The body of a turn: given its run, drive the agent and emit frames onto it.
TurnBody = Callable[["TurnRun"], Awaitable[None]]
# Called with (run, task) once the run's task settles, on the daemon loop.
TurnDone = Callable[["TurnRun", "asyncio.Task"], None]


@dataclass
class TurnRun:
    """One executing conversation turn, owned by the daemon."""

    run_id: str
    thread_id: str
    channel: "ThreadChannel"
    session_id: str | None = None
    origin: str = "cli"
    agent: str = "master"
    card_id: str | None = None
    # "text" | "voice" — voice runs also synthesize the reply as audio frames.
    kind: str = "text"
    # The user text this turn is answering. Replayed in `turn_start` so a client
    # that attaches mid-turn can render the user bubble it never saw.
    text: str = ""
    task: asyncio.Task | None = None
    # "running" | "done" | "cancelled" | "error"
    status: str = "running"
    error: str | None = None
    # The in-flight HITL interrupt, if any. Lives on the RUN (not the socket) so
    # an ask can be answered from a different connection than the one that saw
    # it — and so a reattaching viewer can be told one is outstanding.
    pending_ask: "asyncio.Future[str] | None" = None
    pending_ask_id: str | None = None
    # Channel seq immediately before this run's first frame — the replay floor
    # for a viewer that has no prior state but wants the in-flight turn.
    start_seq: int = 0
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"

    def emit(self, frame: dict[str, Any]) -> int:
        """Stamp a seq, ring it, fan it out. Never blocks, never awaits."""
        return self.channel.emit(frame)

    def answer_ask(self, text: str) -> bool:
        """Resolve the outstanding ask; False when there isn't one."""
        fut = self.pending_ask
        if fut is None or fut.done():
            return False
        fut.set_result(text)
        return True

    def cancel(self) -> bool:
        """Request cancellation of the turn task; False when already settled."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            return True
        return False

    def to_wire(self) -> dict[str, Any]:
        """Compact view for the WS ``hello`` frame / the cancel endpoint."""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "kind": self.kind,
            "origin": self.origin,
            "agent": self.agent,
            "card_id": self.card_id,
            "text": self.text,
            "start_seq": self.start_seq,
            "seq": self.channel.seq,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "pending_ask_id": self.pending_ask_id,
        }


class ThreadChannel:
    """The replay ring + viewer set for one conversation thread."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.seq = 0
        self.ring: deque[dict[str, Any]] = deque(maxlen=TURN_RING_SIZE)
        # Unbounded queues: prose must never be dropped, and a viewer whose
        # socket has genuinely died is detached by its pump task's teardown.
        self.subscribers: set[asyncio.Queue] = set()
        self.run: TurnRun | None = None
        self.touched_at = time.time()

    def emit(self, frame: dict[str, Any]) -> int:
        self.seq += 1
        out = dict(frame)
        out["seq"] = self.seq
        ephemeral = out.get("type") in EPHEMERAL_TYPES
        if not ephemeral:
            self.ring.append(out)
        for q in list(self.subscribers):
            if ephemeral and q.qsize() > EPHEMERAL_BACKLOG:
                continue
            q.put_nowait(out)
        return self.seq

    def attach(
        self, since_seq: int | None = None
    ) -> tuple[list[dict[str, Any]], asyncio.Queue, int]:
        """Subscribe a viewer and hand back the frames it missed.

        ``since_seq=None`` means "I have no prior state": replay only the
        *in-flight* turn (so reopening a pane mid-answer shows it) rather than
        the whole ring, which the client already has as session history.
        Otherwise replay everything after ``since_seq``.

        Race-free by construction: the queue is registered *before* the ring is
        snapshotted and there is no ``await`` between them, so no frame can land
        in exactly one of the two.
        """
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        if since_seq is None:
            run = self.run
            floor = run.start_seq if (run is not None and run.running) else self.seq
        else:
            floor = max(0, since_seq)
        replay = [f for f in self.ring if f["seq"] > floor]
        self.touched_at = time.time()
        return replay, q, floor

    def detach(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)
        self.touched_at = time.time()

    def is_sweepable(self, now: float) -> bool:
        if self.subscribers:
            return False
        run = self.run
        if run is not None and run.running:
            return False
        last = max(self.touched_at, (run.ended_at or 0.0) if run is not None else 0.0)
        return (now - last) > FINISHED_RETENTION_SEC


class TurnRegistry:
    """Every live conversation turn in the daemon, keyed by ``thread_id``.

    Also *is* the per-thread turn gate the WS handler used to keep in a bare
    ``set[str]``: a LangGraph checkpoint is single-writer per thread, so two
    turns streaming the same thread concurrently corrupt the message list
    (duplicate leading human message, empty messages the model 400s on).
    :meth:`start` returns ``None`` rather than a second run. Race-free for the
    same reason the old set was: no ``await`` between the check and the insert.
    """

    def __init__(self) -> None:
        self._channels: "OrderedDict[str, ThreadChannel]" = OrderedDict()

    # ------------------------------------------------------------------ #
    # Channels & viewers                                                   #
    # ------------------------------------------------------------------ #

    def channel(self, thread_id: str) -> ThreadChannel:
        chan = self._channels.get(thread_id)
        if chan is None:
            chan = ThreadChannel(thread_id)
            self._channels[thread_id] = chan
            self._sweep()
        else:
            self._channels.move_to_end(thread_id)
        return chan

    def attach(
        self, thread_id: str, since_seq: int | None = None
    ) -> tuple[ThreadChannel, list[dict[str, Any]], asyncio.Queue, int]:
        chan = self.channel(thread_id)
        replay, q, floor = chan.attach(since_seq)
        return chan, replay, q, floor

    def detach(self, thread_id: str, q: asyncio.Queue) -> None:
        chan = self._channels.get(thread_id)
        if chan is not None:
            chan.detach(q)

    # ------------------------------------------------------------------ #
    # Runs                                                                 #
    # ------------------------------------------------------------------ #

    def run(self, thread_id: str) -> TurnRun | None:
        """The current *or most recent* run on this thread (None if swept)."""
        chan = self._channels.get(thread_id)
        return chan.run if chan is not None else None

    def active(self, thread_id: str) -> TurnRun | None:
        """The run currently executing on this thread, if any."""
        run = self.run(thread_id)
        return run if run is not None and run.running else None

    def start(
        self,
        *,
        thread_id: str,
        body: TurnBody,
        session_id: str | None = None,
        origin: str = "cli",
        agent: str = "master",
        card_id: str | None = None,
        kind: str = "text",
        text: str = "",
        on_done: TurnDone | None = None,
    ) -> TurnRun | None:
        """Create and launch a turn on the daemon loop.

        Returns ``None`` when a turn is already running on ``thread_id`` — the
        caller should tell *its own* connection, not the channel (every other
        viewer is happily watching the turn that's already going).
        """
        if self.active(thread_id) is not None:
            return None
        chan = self.channel(thread_id)
        run = TurnRun(
            run_id=uuid.uuid4().hex[:12],
            thread_id=thread_id,
            channel=chan,
            session_id=session_id,
            origin=origin,
            agent=agent,
            card_id=card_id,
            kind=kind,
            text=text,
            start_seq=chan.seq,
        )
        chan.run = run
        run.emit({
            "type": "turn_start",
            "run_id": run.run_id,
            "kind": kind,
            "origin": origin,
            "text": text,
        })
        run.task = asyncio.create_task(
            self._drive(run, body), name=f"turn:{thread_id}:{run.run_id}"
        )
        if on_done is not None:
            run.task.add_done_callback(lambda t, r=run: on_done(r, t))
        logger.info(
            "turn %s started on %s (origin=%s agent=%s kind=%s)",
            run.run_id, thread_id, origin, agent, kind,
        )
        return run

    async def _drive(self, run: TurnRun, body: TurnBody) -> None:
        """Run the body and terminate the run exactly once, however it ends."""
        try:
            await body(run)
            run.status = "done"
        except asyncio.CancelledError:
            run.status = "cancelled"
            run.emit({"type": "log", "text": "(turn cancelled)"})
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced to every viewer
            run.status = "error"
            run.error = str(exc)
            logger.warning(
                "turn %s failed on %s", run.run_id, run.thread_id, exc_info=True
            )
            run.emit({"type": "error", "message": str(exc)})
        finally:
            run.ended_at = time.time()
            fut, run.pending_ask, run.pending_ask_id = run.pending_ask, None, None
            if fut is not None and not fut.done():
                fut.cancel()
            run.emit({
                "type": "turn_end", "run_id": run.run_id, "status": run.status,
            })
            logger.info(
                "turn %s %s on %s (%.1fs)",
                run.run_id, run.status, run.thread_id, run.ended_at - run.started_at,
            )
            self._sweep()

    def cancel(self, thread_id: str) -> bool:
        """Explicit cancel — the only thing that kills a turn."""
        run = self.active(thread_id)
        return run.cancel() if run is not None else False

    async def cancel_and_wait(self, thread_id: str) -> bool:
        """Cancel and wait for the turn to actually settle.

        Needed whenever a *replacement* turn follows immediately (voice
        barge-in, a fresh push-to-talk press): ``cancel()`` only requests it, so
        starting the new turn in the same tick would hit the still-running run
        and be refused. ``asyncio.wait`` (not ``await task``) so a cancelled
        task's ``CancelledError`` can't propagate into the caller.
        """
        run = self.active(thread_id)
        if run is None:
            return False
        run.cancel()
        if run.task is not None:
            await asyncio.wait({run.task})
        return True

    def answer_ask(self, thread_id: str, text: str) -> bool:
        run = self.active(thread_id)
        return run.answer_ask(text) if run is not None else False

    # ------------------------------------------------------------------ #
    # Retention                                                            #
    # ------------------------------------------------------------------ #

    def _sweep(self) -> None:
        now = time.time()
        for tid in [t for t, c in self._channels.items() if c.is_sweepable(now)]:
            self._channels.pop(tid, None)
        while len(self._channels) > MAX_TRACKED_THREADS:
            for tid, chan in list(self._channels.items()):
                if not chan.subscribers and not (chan.run and chan.run.running):
                    self._channels.pop(tid, None)
                    break
            else:
                break  # everything left is live — let the bound stretch

    async def aclose(self) -> None:
        """Cancel every in-flight turn (daemon shutdown)."""
        runs = [c.run for c in self._channels.values() if c.run and c.run.running]
        for run in runs:
            run.cancel()
        tasks = {r.task for r in runs if r.task is not None}
        if tasks:
            await asyncio.wait(tasks)
        self._channels.clear()
