"""Session objects behind the ``/ws/converse`` socket.

The router's job is to accept a connection and hand it to one of these; the
session owns the frame loop and the state that loop mutates. `R-RDB-01`: every
frame type used to be a branch inside one 801-line function, so a change to one
frame's handling meant reading all of them and none could be tested alone.

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
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

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
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
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
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            if self._persist is not None and not self._cancel.is_set():
                if self._final_text or self._pcm_parts:
                    await self._persist(
                        role="assistant",
                        modality="audio" if self._pcm_parts else "text",
                        text=self._final_text,
                        pcm=b"".join(self._pcm_parts) if self._pcm_parts else None,
                        rate=self._rate,
                    )
