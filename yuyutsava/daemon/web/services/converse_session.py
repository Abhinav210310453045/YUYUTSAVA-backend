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
from typing import Any, Protocol

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

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
