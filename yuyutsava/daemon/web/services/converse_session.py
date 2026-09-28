"""Session objects behind the ``/ws/converse`` socket.

The router's job is to accept a connection and hand it to one of these; the
session owns the frame loop and the state that loop mutates, one handler per
frame type (`R-RDB-01`), so each can be read and tested alone.

:class:`DictationSession` is the first one moved. Dictation genuinely is its own
session type — it opens no conversation, runs no turn, and has no asks or
barge-in — so it shares nothing with a conversation but the voice plumbing, and
that arrives injected.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from yuyutsava.aio.tasks import cancel_and_reap, reap
from yuyutsava.ports.channels import AskPrompt, DecisionConflictError
from yuyutsava.voice.sentence import SentenceChunker

logger = logging.getLogger("yuyutsava.daemon.web.services.converse_session")


class VadLike(Protocol):
    """What the session needs of a VAD result."""

    speech_started: bool
    utterance: bytes | None


class TranscriptLike(Protocol):
    """What the session needs of a transcription."""

    text: str
    confidence: float | None


class VoiceLike(Protocol):
    """The slice of ``VoicePipeline`` a dictation session uses.

    Declared here so the session can be driven by a stub without the pipeline's
    models — and so the router stays the only place that builds a real one.
    """

    async def prewarm(self) -> None: ...
    def feed_audio(self, pcm: bytes) -> VadLike: ...
    async def transcribe_detailed(self, pcm: bytes) -> TranscriptLike: ...
    def flush(self) -> bytes | None: ...
    def close(self) -> None: ...


class DictationSession:
    """Transcribe-only loop for ``?mode=dictate`` (STT dictation, no agent).

    Reuses the exact voice plumbing of a conversation — VAD → STT — but never
    opens a session and never runs a turn. Utterances are transcribed strictly
    in capture order by a single worker (transcription can take seconds; the
    receive loop must keep feeding the VAD meanwhile), each non-empty transcript
    is sent as its own ``transcript`` frame, and the ``audio_end`` flush is
    acknowledged with ``dictate_done`` once the tail has drained. Low-confidence
    transcripts are dropped like the conversation path drops them — hallucinated
    noise is worse than a gap in an editable textarea.
    """

    def __init__(self, ws: WebSocket, voice: VoiceLike, *, min_confidence: float) -> None:
        self._ws = ws
        self._voice = voice
        self._min_confidence = min_confidence
        self._send_lock = asyncio.Lock()
        self._utterances: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def _send(self, obj: dict[str, Any]) -> None:
        async with self._send_lock:
            await self._ws.send_text(json.dumps(obj, default=str))

    async def _worker(self) -> None:
        """Transcribe queued utterances in capture order until the sentinel."""
        while True:
            utt = await self._utterances.get()
            if utt is None:
                await self._send({"type": "dictate_done"})
                return
            result = await self._voice.transcribe_detailed(utt)
            text = result.text
            conf = result.confidence
            if (
                text
                and conf is not None
                and self._min_confidence > 0
                and conf < self._min_confidence
            ):
                logger.info(
                    "dictate: low-confidence transcript dropped (%.2f) %r",
                    conf,
                    text[:80],
                )
                continue
            if text:
                logger.info("dictate: transcript=%r", text[:120])
                await self._send({"type": "transcript", "text": text})

    async def _on_audio(self, msg: dict[str, Any]) -> None:
        b64 = msg.get("pcm")
        if not b64:
            return
        try:
            pcm = base64.b64decode(b64)
        except (ValueError, TypeError):
            return
        res = self._voice.feed_audio(pcm)
        if res.speech_started:
            await self._send({"type": "speech_started"})
        if res.utterance is not None:
            self._utterances.put_nowait(res.utterance)

    def _on_audio_end(self) -> None:
        """End of capture: flush the VAD tail, then let the worker drain in order
        and ack with ``dictate_done`` behind the last transcript."""
        tail = self._voice.flush()
        if tail:
            self._utterances.put_nowait(tail)
        self._utterances.put_nowait(None)

    async def run(self) -> None:
        prewarm = asyncio.create_task(self._voice.prewarm())
        worker = asyncio.create_task(self._worker())
        await self._send({"type": "hello", "mode": "dictate"})
        try:
            while True:
                raw = await self._ws.receive_text()
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    await self._send({"type": "error", "message": "invalid JSON frame"})
                    continue
                mtype = msg.get("type")

                if mtype == "ping":
                    await self._send({"type": "pong"})
                    continue

                if mtype == "audio":
                    await self._on_audio(msg)
                    continue

                if mtype == "audio_end":
                    self._on_audio_end()
                    continue

                await self._send({"type": "error", "message": f"unknown message type {mtype!r}"})

        except WebSocketDisconnect:
            pass
        finally:
            prewarm.cancel()
            await cancel_and_reap(worker, what="converse frame worker")
            self._voice.close()


# The size of one streamed audio frame. Big enough that a reply is a handful of
# messages rather than hundreds, small enough that playback starts promptly.
AUDIO_FRAME_BYTES = 16_000


class SpeakingVoice(VoiceLike, Protocol):
    """A pipeline that can also talk back.

    ``set_speaking`` tightens the VAD for the duration of the agent's own reply
    so its TTS cannot self-interrupt through the microphone.
    """

    async def synthesize(self, text: str) -> tuple[bytes, int]: ...
    def set_speaking(self, speaking: bool) -> None: ...


@dataclass
class SpeakingState:
    """What the frame loop needs to know about the agent's own voice.

    Lives on the connection rather than the turn: with barge-in off, the audio
    branch drops mic input for the whole window in which the agent is answering
    — thinking, speaking, and a short tail while buffered audio drains — so it
    must still be able to read these once the turn that wrote them is gone.
    """

    agent_active: bool = False
    speaking: bool = False
    speaking_since: float = 0.0
    speaking_ended_at: float = 0.0


@dataclass
class TurnPlumbing:
    """The per-connection machinery a turn body needs, whatever its modality.

    Passed as one object because these five always travel together; threading
    them individually put the turn runner over the parameter limit and said
    nothing extra.
    """

    convo: Any
    origin: str
    event_sink: Callable[[Any], Callable[[Any], Awaitable[None]]]
    ask_handler: Callable[[Any], Callable[[Any], Awaitable[str]]]
    meter_frames: Callable[[Any], Any]


class SpokenReply:
    """One turn whose prose is streamed back as audio while it is written.

    Tokens are accumulated into whole sentences and synthesized on a side queue,
    so audio starts on sentence one while the model is still writing. A
    cancellation — barge-in, or the Stop button — stops the turn and anything
    queued or in flight immediately, and the partial clip is discarded rather
    than persisted.
    """

    def __init__(
        self,
        run: Any,
        *,
        voice: SpeakingVoice,
        state: SpeakingState,
        plumbing: TurnPlumbing,
        persist: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._run = run
        self._voice = voice
        self._state = state
        self._plumbing = plumbing
        self._persist = persist
        self._chunker = SentenceChunker()
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._cancel = asyncio.Event()
        # Buffered so a clean turn persists as one replayable row; a barge-in
        # leaves a partial clip, which is dropped.
        self._pcm_parts: list[bytes] = []
        self._rate: int | None = None
        self._final_text = ""

    async def _send_audio(self, pcm: bytes, rate: int) -> None:
        for i in range(0, len(pcm), AUDIO_FRAME_BYTES):
            if self._cancel.is_set():
                return
            frame = pcm[i : i + AUDIO_FRAME_BYTES]
            # Ephemeral: fanned out live to every viewer, never ringed (a turn's
            # PCM is megabytes). The persisted WAV is the replay path.
            self._run.emit(
                {
                    "type": "audio_chunk",
                    "pcm": base64.b64encode(frame).decode("ascii"),
                    "sample_rate": rate,
                }
            )
            await asyncio.sleep(0)  # let the pumps drain between frames

    async def _tts_worker(self) -> None:
        spoke = False
        while True:
            sentence = await self._queue.get()
            try:
                if sentence is None:
                    break
                if self._cancel.is_set():
                    continue
                pcm, rate = await self._voice.synthesize(sentence)
                if not pcm:
                    logger.warning(
                        "voice[%s]: TTS produced no audio for %r — check PIPER_MODEL / `say`",
                        self._plumbing.origin,
                        sentence[:80],
                    )
                if pcm and not self._cancel.is_set():
                    if not spoke:
                        self._run.emit({"type": "speaking_start"})
                        spoke = True
                        self._state.speaking = True
                        self._state.speaking_since = time.monotonic()
                        self._voice.set_speaking(True)
                    await self._send_audio(pcm, rate)
                    self._pcm_parts.append(pcm)
                    self._rate = rate
            finally:
                self._queue.task_done()
        if spoke:
            self._run.emit({"type": "speaking_end"})
        self._state.speaking = False
        self._state.speaking_ended_at = time.monotonic()
        self._voice.set_speaking(False)

    async def _on_event(self, ev: Any, sink: Callable[[Any], Awaitable[None]]) -> None:
        await sink(ev)
        if ev.kind == "token":
            for sentence in self._chunker.feed(ev.data.get("text", "")):
                await self._queue.put(sentence)
        elif ev.kind == "final":
            self._final_text = ev.data.get("text", "") or self._final_text
            rest = self._chunker.flush()
            if rest:
                await self._queue.put(rest)

    async def run(self) -> None:
        """Body for a spoken turn — the shape ``TurnRegistry`` expects."""
        plumbing = self._plumbing
        self._state.agent_active = True
        worker = asyncio.create_task(self._tts_worker())
        sink = plumbing.event_sink(self._run)

        async def _on_event(ev: Any) -> None:
            await self._on_event(ev, sink)

        try:
            async with plumbing.meter_frames(self._run):
                await plumbing.convo.run_turn(
                    self._run.text,
                    on_event=_on_event,
                    ask_handler=plumbing.ask_handler(self._run),
                    run_name=f"{plumbing.origin}-voice",
                    keep_full_payloads=True,
                    modality="voice",
                )
        except asyncio.CancelledError:
            self._cancel.set()
            raise
        finally:
            self._state.agent_active = False
            await self._queue.put(None)
            await reap(worker, what="spoken-reply synth worker")
            if self._persist is not None and not self._cancel.is_set():
                if self._final_text or self._pcm_parts:
                    await self._persist(
                        role="assistant",
                        modality="audio" if self._pcm_parts else "text",
                        text=self._final_text,
                        pcm=b"".join(self._pcm_parts) if self._pcm_parts else None,
                        rate=self._rate,
                    )


@dataclass(frozen=True)
class Heard:
    """What one captured utterance turned out to be."""

    text: str
    confidence: float | None

    @property
    def words(self) -> str:
        return (self.text or "").strip()


@dataclass
class MicDeps:
    """The connection-side calls the mic router makes.

    One object because all five belong to the same socket and always travel
    together; passing them individually puts the router over the argument limit
    and reads as a list of unrelated hooks.
    """

    voice_of: Callable[[], Any]
    send: Callable[[dict[str, Any]], Awaitable[None]]
    start_turn: Callable[..., Awaitable[None]]
    persist: Callable[..., Awaitable[None]]
    spawn: Callable[[Any], "asyncio.Task[Any]"]


class UtteranceRouter:
    """Decides what a captured utterance becomes: a turn, a queued follow-up, or nothing.

    The VAD hands over completed utterances and this routes each one. Both of
    its paths transcribe first and apply the same confidence gate — garbled ASR
    must not reach the agent — which is why that gate lives here once instead of
    being written out at each call site.

    The two paths differ in what silence means. A deliberate utterance that
    transcribes to nothing still has to release the UI, so it answers with
    ``turn_end``; a talk-over that transcribes to nothing is room noise or the
    agent's own echo, so it is dropped and the reply is left to finish. Both
    treat a whitespace-only transcript as silence.
    """

    def __init__(
        self,
        deps: MicDeps,
        *,
        origin: str,
        min_confidence: float,
        barge_in_enabled: bool,
    ) -> None:
        self._deps = deps
        self._origin = origin
        self._min_confidence = min_confidence
        self._barge_in_enabled = barge_in_enabled
        # Continuous (hands-free) voice: the mic stays live across turns, so a
        # completed utterance spoken during a running turn is kept (latest wins)
        # and run the moment that turn ends, rather than dropped. Nothing the
        # user says is lost and they never wait for the mic to "stop".
        self._queued: bytes | None = None
        # Transcription of a captured utterance. Per-connection — it feeds on
        # this socket's pipeline; only the turn it leads to is daemon-owned.
        self._task: asyncio.Task[Any] | None = None
        # True while a candidate talk-over is being transcribed, so several
        # overlapping guesses are not evaluated at once.
        self._barge_pending = False

    # -- state the frame loop consults ---------------------------------- #

    def pending(self) -> bool:
        """True while an utterance is still being transcribed."""
        return self._task is not None and not self._task.done()

    def queue(self, pcm: bytes) -> None:
        """Keep an utterance spoken over a running turn; latest wins."""
        self._queued = pcm

    def on_turn_done(self, run: Any, task: "asyncio.Task[Any]") -> None:
        """Run whatever was said during the turn that just finished."""
        utt, self._queued = self._queued, None
        if utt is None or task.cancelled():
            # A cancelled turn is a barge-in that already started a fresh turn;
            # don't double-run.
            return
        self.hear(utt)

    def hear(self, pcm: bytes, *, preempt: bool = False) -> None:
        """Transcribe-then-run, replacing any utterance still transcribing."""
        if self.pending() and self._task is not None:
            self._task.cancel()
        self._task = self._deps.spawn(self._run_from_utterance(pcm, preempt=preempt))

    # -- the two routes ------------------------------------------------- #

    async def _transcribe(self, pcm: bytes) -> Heard | None:
        voice = self._deps.voice_of()
        if voice is None:
            return None
        result = await voice.transcribe_detailed(pcm)
        return Heard(text=result.text, confidence=result.confidence)

    def _too_uncertain(self, heard: Heard) -> bool:
        """A low-confidence transcript is garbled ASR, not a request.

        Only gates backends that report a confidence; ``None`` (e.g. Groq)
        always passes.
        """
        conf = heard.confidence
        return conf is not None and self._min_confidence > 0 and conf < self._min_confidence

    async def _run_from_utterance(self, pcm: bytes, *, preempt: bool = False) -> None:
        """Transcribe a completed utterance, echo it as the user turn, run it."""
        logger.info("voice[%s]: utterance %d bytes → transcribing", self._origin, len(pcm))
        heard = await self._transcribe(pcm)
        if heard is None:
            return
        if not heard.words:
            # Nothing intelligible — release the UI without a turn. Logged so a
            # "no response" symptom is traceable to empty STT vs. a failed turn.
            logger.info("voice[%s]: empty transcript — no turn run", self._origin)
            await self._deps.send({"type": "turn_end"})
            return
        if self._too_uncertain(heard):
            logger.info(
                "voice[%s]: low-confidence transcript (%.2f < %.2f) %r — asking to repeat",
                self._origin,
                heard.confidence,
                self._min_confidence,
                heard.words[:120],
            )
            await self._deps.send(
                {
                    "type": "clarify",
                    "reason": "low_confidence",
                    "confidence": round(heard.confidence or 0.0, 3),
                    "heard": heard.words,
                    "message": "I didn't quite catch that — could you say it again?",
                }
            )
            await self._deps.send({"type": "turn_end"})
            return
        logger.info("voice[%s]: transcript=%r → running turn", self._origin, heard.words[:120])
        # Mic artifacts stay on this socket; the run's `turn_start` frame carries
        # the same text to every other viewer.
        await self._deps.send({"type": "transcript", "text": heard.words})
        # The user side is persisted as text (the STT transcript). Raw user audio
        # is intentionally not stored by default — privacy + size.
        await self._deps.persist(role="user", modality="text", text=heard.words)
        await self._deps.start_turn(heard.words, preempt=preempt)

    async def maybe_barge_in(self, pcm: bytes) -> None:
        """Interrupt the agent's spoken reply only for genuine talk-over.

        Called when a full utterance is captured *while the agent is speaking*.
        Cancelling on a raw VAD onset is what makes a reply cut out mid-sentence,
        because room noise and the agent's own TTS echo trip it constantly — so
        the utterance is transcribed first and only real, confident words
        interrupt.
        """
        if not self._barge_in_enabled or self._barge_pending:
            return
        self._barge_pending = True
        try:
            try:
                heard = await self._transcribe(pcm)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a failed guess must not kill the reply
                logger.debug("voice[%s]: barge transcription failed", self._origin, exc_info=True)
                return
            if heard is None:
                return
            if not heard.words:
                logger.info(
                    "voice[%s]: talk-over ignored — noise/echo while speaking", self._origin
                )
                return
            if self._too_uncertain(heard):
                logger.info(
                    "voice[%s]: talk-over ignored — low confidence (%.2f) %r",
                    self._origin,
                    heard.confidence,
                    heard.words[:80],
                )
                return
            # Real talk-over: stop the agent's audio + turn, then answer the new
            # utterance. speech_started tells the client to drop the stale audio.
            logger.info("voice[%s]: barge-in confirmed → %r", self._origin, heard.words[:80])
            await self._deps.send({"type": "speech_started"})
            await self._deps.send({"type": "transcript", "text": heard.words})
            await self._deps.persist(role="user", modality="text", text=heard.words)
            await self._deps.start_turn(heard.words, preempt=True)
        finally:
            self._barge_pending = False


@dataclass(frozen=True)
class AskOwnership:
    """Who an inline ask belongs to.

    An ask may render *inline* only in the conversation that raised it —
    everywhere else it is a notification plus an Inbox entry. A permission
    prompt must never appear inside somebody's other running session, which is
    what these fields carry onto the wire record.
    """

    session_id: str
    thread_id: str
    origin: str
    surface: str
    card_id: str | None
    agent_label: str | None


class InlineAsks:
    """Turns a graph interrupt into a prompt, and waits for the answer.

    Where the answer comes back from depends on whether a channel router is
    wired. With one, the ask joins the hub: persisted before broadcast, visible
    on every surface, and answerable from any of them — first answer anywhere
    wins. Without one (tests, embedded use) it falls back to a future on the run
    itself, which the conversation socket resolves directly.

    Either way the prompt is announced and retired on this thread, so a card
    answered in the Inbox does not sit dead in the chat.
    """

    def __init__(
        self,
        ownership: AskOwnership,
        *,
        channels: Any | None,
        describe: Callable[[dict[str, Any]], tuple[str, str, list[str]]],
    ) -> None:
        self._own = ownership
        self._channels = channels
        self._describe = describe

    def handler(self, run: Any) -> Callable[[Any], Awaitable[str]]:
        """The ``ask_handler`` for one run."""

        async def _ask(interrupt_value: Any) -> str:
            iv = (
                dict(interrupt_value)
                if isinstance(interrupt_value, dict)
                else {"type": "user_question", "question": str(interrupt_value)}
            )
            title, body, options = self._describe(iv)
            ask = AskPrompt(
                ask_id=str(uuid.uuid4()),
                title=title,
                body=body,
                options=options,
                interrupt_value=iv,
                session_id=self._own.session_id,
                agent_path=iv.get("agent_path") or self._own.origin,
                surface=self._own.surface,
                thread_id=self._own.thread_id,
                card_id=self._own.card_id,
                agent_label=self._own.agent_label,
                interrupt_id=iv.get("interrupt_id"),
            )
            run.pending_ask_id = ask.ask_id
            # Inline frame for whoever is viewing this thread — the same wire
            # record the SSE/inbox/overlay surfaces receive, so all of them
            # render from one shape through one shared card.
            run.emit({"type": "ask", "ask": ask.to_wire_dict()})
            try:
                if self._channels is not None:
                    return await self._channels.post_ask(ask)
                loop = asyncio.get_running_loop()
                fut: asyncio.Future[str] = loop.create_future()
                run.pending_ask = fut
                return await fut
            finally:
                run.pending_ask = None
                run.pending_ask_id = None
                # However it was answered — inline here, from the Inbox, from
                # the overlay, or by the turn being cancelled — every viewer of
                # this thread clears its inline card. Without this, answering in
                # the Inbox would leave a dead prompt sitting in the chat.
                run.emit({"type": "ask_resolved", "ask_id": ask.ask_id})

        return _ask


@dataclass
class FrameDeps:
    """What the frame loop needs from the connection it serves.

    One object because every field belongs to the same socket; as separate
    arguments this is a twelve-parameter constructor that reads as a pile of
    unrelated hooks.
    """

    send: Callable[[dict[str, Any]], Awaitable[None]]
    registry: Any
    thread_id: str
    origin: str
    decisions: Any | None
    start_turn: Callable[..., bool]
    reject_busy: Callable[[], Awaitable[None]]
    ensure_voice: Callable[[], Any]
    voice_of: Callable[[], Any]
    speaking: SpeakingState
    utterances: UtteranceRouter
    spawn: Callable[[Any], "asyncio.Task[Any]"]


class ConverseFrames:
    """The inbound frame protocol for a conversation socket.

    One handler per frame type (`R-RDB-01`), so changing the handling of one
    means reading only that one, and each can be exercised on its own.

    The loop itself only parses and dispatches. Anything a frame *does* — start
    a turn, resolve an ask, route an utterance — belongs to a collaborator, so
    this stays a description of the protocol rather than of the agent.
    """

    def __init__(
        self,
        ws: WebSocket,
        deps: FrameDeps,
        *,
        barge_in_enabled: bool,
        post_speak_grace: float,
    ) -> None:
        self._ws = ws
        self._d = deps
        self._barge_in_enabled = barge_in_enabled
        self._post_speak_grace = post_speak_grace

    async def run(self) -> None:
        """Read frames until the client goes away."""
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
            "ping": self._on_ping,
            "ask_response": self._on_ask_response,
            "interrupt": self._on_interrupt,
            "user_text": self._on_user_text,
            "retry": self._on_retry,
            "audio": self._on_audio,
            "audio_end": self._on_audio_end,
        }
        try:
            while True:
                raw = await self._ws.receive_text()
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    await self._d.send({"type": "error", "message": "invalid JSON frame"})
                    continue
                handler = handlers.get(msg.get("type"))
                if handler is None:
                    await self._d.send(
                        {"type": "error", "message": f"unknown message type {msg.get('type')!r}"}
                    )
                    continue
                await handler(msg)
        except WebSocketDisconnect:
            pass

    # ------------------------------------------------------------------ #
    # One handler per frame type                                          #
    # ------------------------------------------------------------------ #

    async def _on_ping(self, msg: dict[str, Any]) -> None:
        await self._d.send({"type": "pong"})

    async def _on_interrupt(self, msg: dict[str, Any]) -> None:
        self._d.registry.cancel(self._d.thread_id)

    async def _on_user_text(self, msg: dict[str, Any]) -> None:
        text = (msg.get("text") or "").strip()
        if not text:
            return
        # Board-UI selection context (objective/note reference ids) rides a
        # structured field and is composed here — the sentinel wrapper is
        # canonical server-side, and clients strip it from hydrated transcripts
        # so the user only ever sees their own text.
        context = (msg.get("context") or "").strip()
        if context:
            text = f"<selection-context>\n{context}\n</selection-context>\n\n{text}"
        if not self._d.start_turn(text, spoken=False):
            await self._d.reject_busy()

    async def _on_retry(self, msg: dict[str, Any]) -> None:
        """A notice's Retry: the model runs again on the history, nothing is sent."""
        if not self._d.start_turn("", spoken=False, retry=True):
            await self._d.reject_busy()

    async def _on_ask_response(self, msg: dict[str, Any]) -> None:
        text = str(msg.get("text", ""))
        run = self._d.registry.active(self._d.thread_id)
        ask_id = msg.get("ask_id") or (run.pending_ask_id if run else None)
        # Answer whichever mechanism is actually waiting. `InlineAsks` picks its
        # wait on `channels`; deciding here on `decisions` alone would disagree
        # with it, because `create_app` always builds a DecisionService but
        # `channels` may be absent (tests, embedded). In that pairing the turn
        # parks on a future nobody resolves — no `final`, no `turn_end`, forever
        # (`R-BUG-08`). `pending_ask` is set only by that run-local branch, so a
        # future that accepts the answer identifies it exactly.
        if run is not None and run.answer_ask(text):
            return
        # The hub path: an answer given here is indistinguishable from one given
        # in the Inbox or the overlay — same record, same resolution, same
        # ask_resolved broadcast clearing every surface.
        if ask_id and self._d.decisions is not None:
            # An ask is visible here before the hub registers the future that
            # carries its answer, so answering immediately can beat the waiter.
            # This run raised this ask and is still blocked on it, so the answer
            # is worth holding rather than rejecting (`R-BUG-09`).
            in_flight = run is not None and run.pending_ask_id == ask_id
            try:
                await self._d.decisions.respond_ask(str(ask_id), text, hold_if_unwaited=in_flight)
            except DecisionConflictError:
                # Someone answered it elsewhere first — that's the design, not
                # an error. Tell this socket so it clears.
                await self._d.send({"type": "ask_resolved", "ask_id": ask_id})

    async def _on_audio(self, msg: dict[str, Any]) -> None:
        b64 = msg.get("pcm")
        if not b64:
            return
        try:
            pcm = base64.b64decode(b64)
        except (ValueError, TypeError):
            return
        voice = self._d.ensure_voice()
        turn_running = self._d.registry.active(self._d.thread_id) is not None
        if self._mic_is_gated(turn_running):
            return
        res = voice.feed_audio(pcm)
        if res.speech_started and not self._d.speaking.speaking:
            # A raw speech ONSET never interrupts the agent. While it is
            # SPEAKING an onset is almost always room noise or the agent's own
            # TTS echo, and cutting the reply there is exactly the "it stops
            # mid-sentence" bug; a genuine talk-over is handled below, once a
            # full utterance transcribes to real words. While the agent is
            # THINKING or idle this only raises the listening indicator, which
            # does not cancel anything, so the user can see they were heard.
            await self._d.send({"type": "speech_started"})
        if res.utterance is not None:
            self._route_utterance(res.utterance, turn_running=turn_running)

    async def _on_audio_end(self, msg: dict[str, Any]) -> None:
        """Explicit end of capture (push-to-talk release): flush the VAD."""
        voice = self._d.voice_of()
        if voice is None:
            return
        utt = voice.flush()
        if utt:
            self._d.utterances.hear(utt, preempt=True)

    # ------------------------------------------------------------------ #
    # The two decisions the audio frame makes                             #
    # ------------------------------------------------------------------ #

    def _mic_is_gated(self, turn_running: bool) -> bool:
        """Whether mic input is being ignored right now.

        Unless barge-in is explicitly enabled, input is dropped for the WHOLE
        duration of an active turn — thinking, speaking, and a short tail while
        buffered audio drains — so the agent's own TTS echo or room noise cannot
        cut the reply off or queue a spurious follow-up. The answer is always
        heard in full; the user interrupts with the Stop button instead. (The
        client also mutes its mic while the reply is still PLAYING — see the
        `barge_in` field of `hello` — which is the authoritative guard, since
        only the client knows when playback truly ends.)
        """
        if self._barge_in_enabled:
            return False
        speaking = self._d.speaking
        return (
            turn_running
            or self._d.utterances.pending()
            or speaking.agent_active
            or speaking.speaking
            or (time.monotonic() - speaking.speaking_ended_at) < self._post_speak_grace
        )

    def _route_utterance(self, utterance: bytes, *, turn_running: bool) -> None:
        """Talk-over, queued follow-up, or a turn right now."""
        speaking = self._d.speaking
        if speaking.speaking:
            # Possible talk-over. Don't cancel yet — verify it is real speech
            # (transcribe-before-cancel) so noise and echo cannot chop the reply
            # off. Runs off to the side while the agent keeps speaking; only
            # confirmed words actually interrupt.
            self._d.spawn(self._d.utterances.maybe_barge_in(utterance))
        elif speaking.agent_active or turn_running:
            # The agent is mid-answer (thinking) — don't interrupt it, but don't
            # drop what the user just said either. Queue this completed
            # utterance (latest wins) so it runs the moment the current turn
            # ends, keeping the mic continuously useful.
            self._d.utterances.queue(utterance)
            logger.info("voice[%s]: utterance queued — runs after current turn", self._d.origin)
        else:
            # Idle, or only transcribing a prior (possibly-empty noise)
            # utterance — let this newer one win: it cancels the stale
            # transcription and runs this instead.
            self._d.utterances.hear(utterance)
