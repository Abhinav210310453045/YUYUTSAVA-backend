"""One ``/ws/converse`` conversation connection, from handshake to teardown.

The router's remaining job is to accept the socket, read the query string and
open a conversation; everything after that — the handshake, the replay, the
frame pump, starting turns, and closing down without cutting a reply off — is
this class (`R-RDB-01`).

A connection is only ever a **viewer** of its thread. Turns live in the daemon's
:class:`~yuyutsava.daemon.turn_registry.TurnRegistry`, keyed by thread, and
several sockets (a reopened pane, the overlay, a second window) can watch the
same one. Nothing here owns a run, which is why disconnecting detaches rather
than cancels.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import WebSocket

from yuyutsava.context.meter import bus
from yuyutsava.core.streaming import StreamEvent
from yuyutsava.daemon.turn_registry import TurnRun
from yuyutsava.daemon.web.services.converse_session import (
    AskOwnership,
    ConverseFrames,
    FrameDeps,
    InlineAsks,
    MicDeps,
    SpeakingState,
    SpokenReply,
    TurnPlumbing,
    UtteranceRouter,
)
from yuyutsava.storage.voice_blobs import write_voice_wav

logger = logging.getLogger("yuyutsava.daemon.web.services.converse_connection")


@dataclass(frozen=True)
class VoicePolicy:
    """How this deployment treats the microphone."""

    barge_in_enabled: bool
    post_speak_grace: float
    min_confidence: float


@dataclass(frozen=True)
class ConverseSpec:
    """What the router resolved before handing the socket over.

    ``surface`` and ``agent_label`` decide where an ask raised here is allowed
    to render; ``channels`` and ``decisions`` decide how its answer comes back.
    """

    convo: Any
    manager: Any
    origin: str
    agent: str
    card_id: str | None
    surface: str
    agent_label: str
    resuming: bool
    channels: Any | None
    decisions: Any | None
    voice_store: Any | None
    voice_toggles: Any | None


def latest_usage_snapshot(thread_id: str) -> dict | None:
    """Last context-meter reading for a thread, or None. Never raises.

    Telemetry must not be able to fail a handshake, so a bad read degrades to
    "no numbers yet" — which is also what a fresh conversation looks like.
    """
    try:
        snap = bus().latest(thread_id)
        return snap.as_dict() if snap is not None else None
    except Exception:  # noqa: BLE001
        logger.debug("usage snapshot unavailable for hello", exc_info=True)
        return None


class ConverseConnection:
    """One open conversation socket."""

    def __init__(
        self,
        ws: WebSocket,
        spec: ConverseSpec,
        *,
        since_seq: int | None,
        voice_factory: Callable[[], Any],
        policy: VoicePolicy,
        describe_interrupt: Callable[[dict[str, Any]], tuple[str, str, list[str]]],
    ) -> None:
        self._ws = ws
        self._spec = spec
        self._since_seq = since_seq
        # A factory, not a class: the pipeline is built lazily on the first
        # audio frame, and the tests replace it by patching the name in the
        # router module. Resolving it here instead would quietly detach that.
        self._voice_factory = voice_factory
        self._policy = policy
        self._describe = describe_interrupt

        self._registry = spec.manager.turns
        self._thread_id = spec.convo.thread_id
        self._send_lock = asyncio.Lock()
        self._last_sent_seq = 0

        self._voice: Any | None = None
        # Runs THIS connection started that speak through ITS pipeline. The
        # pipeline is genuinely per-connection (it owns mic frames), but closing
        # it out from under a reply still being synthesized would cut the answer
        # off, so on disconnect it is handed to the run to close instead.
        self._voice_runs: set[str] = set()
        # Best-effort background work (model pre-warm, utterance transcription)
        # kept referenced so it isn't GC'd mid-flight; cancelled on disconnect.
        self._bg: set[asyncio.Task] = set()

        # Mic guards, read by the audio branch and written by whichever turn
        # body is running. Connection-scoped: the run they describe may outlive
        # this socket.
        self._speaking = SpeakingState()

        self._asks = InlineAsks(
            AskOwnership(
                session_id=spec.convo.session_id,
                thread_id=self._thread_id,
                origin=spec.origin,
                surface=spec.surface,
                card_id=spec.card_id,
                agent_label=spec.agent_label,
            ),
            channels=spec.channels,
            describe=describe_interrupt,
        )
        self._utterances = UtteranceRouter(
            MicDeps(
                voice_of=lambda: self._voice,
                send=self._send,
                start_turn=self.start_spoken_turn,
                persist=self._persist_voice_message,
                spawn=self._spawn_bg,
            ),
            origin=spec.origin,
            min_confidence=policy.min_confidence,
            barge_in_enabled=policy.barge_in_enabled,
        )

    # ------------------------------------------------------------------ #
    # The socket                                                          #
    # ------------------------------------------------------------------ #

    async def _send(self, obj: dict) -> None:
        """Connection-scoped write: ``hello``, ``pong``, mic state, replay.

        Frames that belong to the *turn* never come through here directly —
        they go onto the run's channel and reach every viewer via the pump.
        """
        async with self._send_lock:
            await self._ws.send_text(json.dumps(obj, default=str))

    def _spawn_bg(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    async def _handshake(self, chan: Any, live_run: TurnRun | None) -> None:
        spec = self._spec
        toggles = spec.voice_toggles
        await self._send(
            {
                "type": "hello",
                "session_id": spec.convo.session_id,
                "thread_id": spec.convo.thread_id,
                "origin": spec.origin,
                "agent": spec.agent,
                "card_id": spec.card_id,
                "resuming": spec.resuming,
                # Where this thread's frame counter stands, and what (if
                # anything) is running on it right now — so a client that
                # reattaches mid-turn stays in its "busy" state instead of
                # releasing the composer.
                "seq": chan.seq,
                "run": live_run.to_wire() if live_run is not None else None,
                # The voice interruption policy, so the client can match it:
                # when barge-in is off (default) the client runs half-duplex,
                # muting the mic while the reply is still playing so noise
                # cannot cut it off.
                "barge_in": self._policy.barge_in_enabled,
                # Voice-mode snapshot at connect. The live source of truth is
                # the `settings` SSE item that every surface already listens to;
                # this is so a freshly-opened panel renders the right affordance
                # before its first SSE frame arrives.
                "voice": (
                    toggles.voice().to_dict()
                    if toggles is not None
                    else {"wake_enabled": True, "tts_enabled": True}
                ),
                # Last known context/spend reading, so a panel opened between
                # turns shows real numbers instead of waiting for the next model
                # call to say anything.
                "usage": latest_usage_snapshot(spec.convo.thread_id),
            }
        )

    async def _pump(self, sub_q: Any) -> None:
        """Relay this thread's live frames onto the socket, in seq order."""
        try:
            while True:
                frame = await sub_q.get()
                seq = frame.get("seq", 0)
                if seq <= self._last_sent_seq:
                    continue  # already replayed at handshake
                self._last_sent_seq = seq
                await self._send(frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a dead socket just ends the pump
            logger.debug("converse: pump ended for %s", self._thread_id, exc_info=True)

    # ------------------------------------------------------------------ #
    # Turns                                                               #
    # ------------------------------------------------------------------ #

    async def _reject_busy(self) -> None:
        """Refuse a second turn — to THIS socket only.

        The thread is already streaming a turn to every viewer; broadcasting a
        "busy" error onto the channel would blank it out for all of them.
        """
        await self._send(
            {"type": "error", "message": "a turn is already running on this conversation"}
        )
        await self._send({"type": "turn_end"})

    @contextlib.asynccontextmanager
    async def _meter_frames(self, run: TurnRun):
        """Forward context-meter snapshots to this conversation's viewers.

        Subscribed per turn rather than per socket so the frames stop when the
        turn does, and scoped to this thread so a second conversation's window
        never lands on this one's panel.

        Not a ``StreamEvent`` kind on purpose: the meter publishes from inside
        the model step, and routing it through the graph stream would mean
        teaching core/streaming, both CLI renderers, the SSE payload union and
        the static web UI about a frame only this handler can use.
        """

        def _forward(snap) -> None:
            # emit() is a put_nowait fan-out; safe to call from the model step.
            run.emit({"type": "usage", **snap.as_dict()})

        token = bus().subscribe(self._spec.convo.thread_id, _forward)
        try:
            yield
        finally:
            bus().unsubscribe(token)

    def _event_sink(self, run: TurnRun):
        spec = self._spec

        async def _sink(ev: StreamEvent) -> None:
            # Link any background task this turn launches back to this
            # conversation thread, so its completion wakes the master here
            # (subagent_completed).
            spec.manager.record_async_launch(ev, thread_id=self._thread_id, origin=spec.origin)
            run.emit({"type": ev.kind, **ev.data})

        return _sink

    async def _run_text_turn(self, run: TurnRun) -> None:
        """Text turn body. Errors, cancellation and ``turn_end`` are the
        registry's job (``TurnRegistry._drive``) — it terminates a run exactly
        once however it ends, for every viewer at the same time."""
        self._speaking.agent_active = True
        try:
            if not self._spec.convo.bundle_ready:
                run.emit({"type": "log", "text": "preparing agent (first run)…"})
            async with self._meter_frames(run):
                await self._spec.convo.run_turn(
                    run.text,
                    on_event=self._event_sink(run),
                    ask_handler=self._asks.handler(run),
                    run_name=f"{self._spec.origin}-chat",
                    keep_full_payloads=True,
                )
        finally:
            self._speaking.agent_active = False

    async def _run_voice_turn(self, run: TurnRun) -> None:
        """Body for a spoken turn — the streaming lives in :class:`SpokenReply`."""
        assert self._voice is not None
        await SpokenReply(
            run,
            voice=self._voice,
            state=self._speaking,
            plumbing=TurnPlumbing(
                convo=self._spec.convo,
                origin=self._spec.origin,
                event_sink=self._event_sink,
                ask_handler=self._asks.handler,
                meter_frames=self._meter_frames,
            ),
            persist=(self._persist_voice_message if self._spec.voice_store is not None else None),
        ).run()

    def start_turn(self, text: str, *, spoken: bool) -> bool:
        """Hand one turn to the daemon. False when the thread is already busy."""
        spec = self._spec
        run = spec.manager.start_turn(
            self._thread_id,
            body=self._run_voice_turn if spoken else self._run_text_turn,
            session_id=spec.convo.session_id,
            origin=spec.origin,
            agent=spec.agent,
            card_id=spec.card_id,
            kind="voice" if spoken else "text",
            text=text,
            on_done=self._utterances.on_turn_done,
        )
        if run is None:
            return False
        if spoken:
            self._voice_runs.add(run.run_id)
        return True

    async def start_spoken_turn(self, text: str, *, preempt: bool = False) -> None:
        """Answer a spoken utterance, aloud or silently per the voice toggle.

        With TTS off the turn still runs exactly as a mic-driven turn — the
        transcript is echoed, the answer streams — it just isn't synthesized.
        That is the "manual mic still works, nothing talks back" mode.
        ``preempt`` waits out the turn it is replacing (barge-in, or a new
        push-to-talk press) so the thread's turn gate doesn't reject the new one.
        """
        if preempt:
            await self._registry.cancel_and_wait(self._thread_id)
        spoken = self._voice is not None and await self._tts_enabled()
        if not self.start_turn(text, spoken=spoken):
            await self._reject_busy()

    async def _tts_enabled(self) -> bool:
        """Whether spoken replies are on right now (voice-mode runtime toggle).

        Read per turn, not per connection: a user who turns voice mode off
        mid-chat should stop being spoken to on the *next* answer without
        reconnecting. A reply already being synthesized finishes — cutting a
        live sentence in half is worse than one extra spoken answer. The refresh
        is TTL-guarded (usually a no-op) and catches a toggle written by another
        process, e.g. ``/voice off`` typed into a CLI REPL. Defaults to on when
        the daemon has no settings wired (tests / embedded use).
        """
        settings = self._spec.voice_toggles
        if settings is None:
            return True
        await settings.refresh()
        return settings.voice().tts_enabled

    # ------------------------------------------------------------------ #
    # Voice plumbing                                                      #
    # ------------------------------------------------------------------ #

    def _ensure_voice(self) -> Any:
        if self._voice is None:
            self._voice = self._voice_factory()
            # Warm STT/TTS off the loop so the first spoken turn isn't a cold
            # model load. Overlaps with the user's first utterance.
            self._spawn_bg(self._voice.prewarm())
        return self._voice

    async def _persist_voice_message(
        self,
        *,
        role: str,
        modality: str,
        text: str,
        pcm: bytes | None = None,
        rate: int | None = None,
    ) -> None:
        """Best-effort append of one turn to the replayable voice history.

        Writes the TTS audio (if any) to disk off the loop, then the DB row.
        Persistence never breaks a live turn — failures are logged and dropped.
        """
        store = self._spec.voice_store
        if store is None:
            return
        try:
            blob_path = None
            if pcm and rate:
                blob_path = await asyncio.to_thread(
                    write_voice_wav, self._spec.convo.thread_id, pcm, rate
                )
            await store.put_message(
                self._spec.convo.thread_id,
                role=role,
                modality=modality,
                text=text or "",
                audio_blob_path=blob_path,
                sample_rate=rate if blob_path else None,
            )
        except Exception:  # noqa: BLE001
            logger.warning("converse: persisting voice message failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Attach, hand over the backlog, then serve frames until disconnect."""
        spec = self._spec
        # Attach as a viewer BEFORE the handshake goes out, so a frame emitted
        # between "read the ring" and "start the pump" can't fall through the gap.
        chan, replay, sub_q, floor = self._registry.attach(self._thread_id, self._since_seq)
        self._last_sent_seq = replay[-1]["seq"] if replay else floor

        await self._handshake(chan, chan.run)
        # The gap this client missed while it was away (or the in-flight turn
        # from its start, for a client with no prior state), oldest first.
        for frame in replay:
            await self._send(frame)

        pump_task = asyncio.create_task(self._pump(sub_q), name=f"converse-pump:{self._thread_id}")
        # Pre-warm the (heavy) agent bundle in the background so the first turn
        # isn't a cold start — overlaps the one-time build with the user
        # speaking. No-op once the shared bundle exists.
        if not spec.convo.bundle_ready:
            self._spawn_bg(spec.convo.prewarm())

        frames = ConverseFrames(
            self._ws,
            FrameDeps(
                send=self._send,
                registry=self._registry,
                thread_id=self._thread_id,
                origin=spec.origin,
                decisions=spec.decisions,
                start_turn=self.start_turn,
                reject_busy=self._reject_busy,
                ensure_voice=self._ensure_voice,
                voice_of=lambda: self._voice,
                speaking=self._speaking,
                utterances=self._utterances,
                spawn=self._spawn_bg,
            ),
            barge_in_enabled=self._policy.barge_in_enabled,
            post_speak_grace=self._policy.post_speak_grace,
        )
        try:
            await frames.run()
        finally:
            await self._close(sub_q, pump_task)

    async def _close(self, sub_q: Any, pump_task: asyncio.Task) -> None:
        # This socket was only ever a VIEWER. Detach — never cancel: the run
        # keeps going on the daemon loop and whichever client comes back next
        # replays the gap with ?since_seq=.
        self._registry.detach(self._thread_id, sub_q)
        pump_task.cancel()
        for task in list(self._bg):
            if not task.done():
                task.cancel()
        self._close_voice()
        await self._retire_session()

    def _close_voice(self) -> None:
        if self._voice is None:
            return
        live = self._registry.active(self._thread_id)
        if live is not None and live.run_id in self._voice_runs and live.task is not None:
            # This connection's pipeline is still synthesizing that reply —
            # closing it now would cut the answer off mid-sentence. Hand it to
            # the run to close when it ends.
            live.task.add_done_callback(lambda _t, v=self._voice: v.close())
        else:
            self._voice.close()

    async def _retire_session(self) -> None:
        """Drop a session that was opened but never used; otherwise idle it.

        A tab or overlay that connects with nothing said (or a flaky reconnect)
        is just clutter — the Sessions history should not fill with empty
        seconds-old rows. Only freshly-created sessions qualify; a resumed one
        is left alone. Skipped entirely while a turn is still running on this
        thread: it is anything but idle, and discarding the row would orphan a
        live run. Best-effort.
        """
        try:
            if self._registry.active(self._thread_id) is not None:
                return
            discarded = (not self._spec.resuming) and await self._spec.convo.discard_if_unused()
            if not discarded:
                await self._spec.convo.finish("idle")
        except Exception:  # noqa: BLE001
            pass
