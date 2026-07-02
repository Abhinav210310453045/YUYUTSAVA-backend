"""Per-connection voice glue for ``WS /ws/converse``: VAD → STT, and TTS → PCM.

A :class:`VoicePipeline` is created per voice WebSocket. It owns the audio-domain
work so the router (``routers/converse.py``) only deals with the protocol and the
turn loop:

* :meth:`feed_audio` runs incoming mic PCM through the VAD and reports speech
  onset / completed utterances.
* :meth:`transcribe` turns a completed utterance into text (STT).
* :meth:`synthesize` turns a sentence of agent text into PCM for the client.

STT/TTS backends are built lazily from the environment and cached; if the voice
extras or models are unavailable the methods degrade (empty transcript / empty
audio) instead of raising, so a text turn on the same socket still works.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import wave
from pathlib import Path
from typing import Callable

from yuyutsava.audio_io.synth import synthesize_pcm
from yuyutsava.audio_io.vad import VadResult, VadSegmenter
from yuyutsava.io.stt import STT, TranscriptResult, stt_from_env
from yuyutsava.io.tts import TTS, tts_from_env

logger = logging.getLogger("yuyutsava.daemon.web.voice_pipeline")

_SAMPLE_RATE = 16_000
# Backstop: ignore utterances shorter than this much audio. The VAD already
# drops noise blips, but this guards against any path that hands us a tiny clip
# (which faster-whisper would otherwise "transcribe" into hallucinated text).
_MIN_TRANSCRIBE_BYTES = int(_SAMPLE_RATE * 0.30) * 2  # ~300 ms of 16 kHz int16


class VoicePipeline:
    """Audio-domain helper for one voice conversation (VAD + STT + TTS)."""

    def __init__(
        self,
        *,
        stt_factory: Callable[[], STT] | None = None,
        tts_factory: Callable[[], TTS] | None = None,
        vad: VadSegmenter | None = None,
    ) -> None:
        self._stt_factory = stt_factory or stt_from_env
        self._tts_factory = tts_factory or tts_from_env
        self._stt: STT | None = None
        self._tts: TTS | None = None
        self._stt_failed = False
        self._tts_failed = False
        # from_env() so the barge-in / noise thresholds are tunable per host
        # (YUYUTSAVA_VAD_BARGE_ENERGY etc.) without a code change.
        self._vad = vad or VadSegmenter.from_env()
        self._tmp = Path(tempfile.mkdtemp(prefix="yuyutsava_voice_ws_"))
        self._seq = 0

    # -- VAD ---------------------------------------------------------------

    def feed_audio(self, pcm: bytes) -> VadResult:
        """Feed mic PCM; returns speech-onset / completed-utterance signals."""
        return self._vad.feed(pcm)

    def reset(self) -> None:
        self._vad.reset()

    async def prewarm(self) -> None:
        """Load STT + TTS models off the event loop, ahead of first use.

        Both backends are otherwise built lazily on the first transcribe/synthesize
        call, adding seconds (model load / first-run download) to the first spoken
        turn. Warming them when the pipeline is created — overlapped with the user
        speaking — keeps that cost off the hot path. Best-effort; degraded backends
        already no-op, so failures here are harmless."""
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._get_stt)
            await loop.run_in_executor(None, self._get_tts)
        except Exception:  # noqa: BLE001 — pre-warm is best-effort
            logger.debug("voice pipeline prewarm failed", exc_info=True)

    def set_speaking(self, speaking: bool) -> None:
        """Tell the VAD the agent is speaking so it gates barge-in against the
        agent's own TTS echo (stricter onset while ``speaking`` is True)."""
        self._vad.set_speaking(speaking)

    def flush(self) -> bytes | None:
        return self._vad.flush()

    # -- STT ---------------------------------------------------------------

    async def transcribe(self, pcm: bytes) -> str:
        """Transcribe an utterance (16 kHz mono int16 PCM). '' on failure."""
        return (await self.transcribe_detailed(pcm)).text

    async def transcribe_detailed(self, pcm: bytes) -> TranscriptResult:
        """Transcribe with confidence. Empty text + ``None`` conf on failure."""
        stt = self._get_stt()
        if stt is None or not pcm or len(pcm) < _MIN_TRANSCRIBE_BYTES:
            return TranscriptResult(text="", confidence=None)
        self._seq += 1
        wav = self._tmp / f"utt_{self._seq}.wav"
        try:
            await asyncio.get_running_loop().run_in_executor(None, _write_wav, wav, pcm)
            result = await stt.transcribe_detailed(wav)
            return TranscriptResult(text=result.text.strip(), confidence=result.confidence)
        except Exception:  # noqa: BLE001
            logger.warning("voice: transcription failed", exc_info=True)
            return TranscriptResult(text="", confidence=None)

    # -- TTS ---------------------------------------------------------------

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        """Synthesize a sentence to (pcm_int16_le, sample_rate). ('',0) on failure."""
        tts = self._get_tts()
        if tts is None:
            return b"", 0
        try:
            return await synthesize_pcm(tts, text)
        except Exception:  # noqa: BLE001
            logger.warning("voice: synthesis failed", exc_info=True)
            return b"", 0

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            import shutil
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    # -- internals ---------------------------------------------------------

    def _get_stt(self) -> STT | None:
        if self._stt is not None or self._stt_failed:
            return self._stt
        try:
            self._stt = self._stt_factory()
        except Exception:  # noqa: BLE001
            self._stt_failed = True
            logger.warning("voice: STT unavailable — captions disabled", exc_info=True)
        return self._stt

    def _get_tts(self) -> TTS | None:
        if self._tts is not None or self._tts_failed:
            return self._tts
        try:
            self._tts = self._tts_factory()
        except Exception:  # noqa: BLE001
            self._tts_failed = True
            logger.warning("voice: TTS unavailable — spoken replies disabled", exc_info=True)
        return self._tts


def _write_wav(path: Path, pcm: bytes, sample_rate: int = _SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
