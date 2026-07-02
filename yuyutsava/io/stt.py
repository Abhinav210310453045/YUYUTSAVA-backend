"""Speech-to-text backends.

Default: ``faster_whisper`` (fully local, privacy-first).
Cloud opt-in: Groq Whisper API (set ``STT_PROVIDER=groq``).

Usage::

    stt = stt_from_env()
    transcript = await stt.transcribe(Path("utterance.wav"))
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("yuyutsava.io.stt")


@dataclass(frozen=True)
class TranscriptResult:
    """A transcript plus an optional ``[0,1]`` confidence.

    ``confidence is None`` means the backend exposes no usable signal (e.g.
    Groq); callers treat that as "no opinion" and do not gate on it.
    """

    text: str
    confidence: float | None = None


def _aggregate_confidence(
    probs: list[float], weights: list[int], nsp: list[float]
) -> float | None:
    """Length-weighted acoustic confidence, discounted by silence likelihood.

    Returns ``None`` when there is nothing to score (empty transcript). High
    ``no_speech_prob`` (silence the decoder still turned into words — the
    hallucination case) pulls the score down so the gate can catch it.
    """
    total_w = sum(weights)
    if not total_w:
        return None
    conf = sum(p * w for p, w in zip(probs, weights)) / total_w
    avg_nsp = (sum(nsp) / len(nsp)) if nsp else 0.0
    return max(0.0, min(1.0, conf * (1.0 - min(0.9, avg_nsp))))


class STT(ABC):
    """Speech-to-text ABC. Implementations return a stripped transcript string."""

    @abstractmethod
    async def transcribe(self, wav_path: Path) -> str: ...

    async def transcribe_detailed(self, wav_path: Path) -> TranscriptResult:
        """Transcript + confidence. Default: text only, no confidence signal.

        Backends with a quality signal (faster-whisper) override this; the plain
        :meth:`transcribe` stays the text-only path so existing callers are
        unaffected.
        """
        return TranscriptResult(text=await self.transcribe(wav_path), confidence=None)


class FasterWhisperSTT(STT):
    """Local transcription via ``faster-whisper``. No network or API key needed."""

    def __init__(
        self,
        model_size: str = "base",
        language: str | None = "en",
        *,
        vad_filter: bool = True,
        vad_threshold: float = 0.5,
    ) -> None:
        self._model_size = model_size
        # Pinning the language stops faster-whisper from mis-detecting short,
        # quiet wake-utterances as Hindi/Norwegian/etc. and hallucinating
        # transcripts ("1kg 1kg 1kg"). None = auto-detect (multilingual users).
        self._language = language or None
        # faster-whisper's built-in Silero VAD drops non-speech before decoding.
        # Its default threshold (0.5) discards quiet/soft speech — the "I have to
        # talk loudly or it transcribes nothing / empty" symptom. Lower the
        # threshold (e.g. 0.2–0.3) to keep softer speech, or turn the filter off
        # entirely and rely on the pipeline VAD for boundaries.
        self._vad_filter = vad_filter
        self._vad_threshold = vad_threshold
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "faster-whisper not installed — run: uv pip install 'yuyutsava[voice]'"
                ) from exc
            self._model = WhisperModel(self._model_size, device="cpu", compute_type="int8")
        return self._model

    def _decode(self, wav_path: Path) -> TranscriptResult:
        model = self._load()
        segments, _ = model.transcribe(
            str(wav_path),
            beam_size=5,
            language=self._language,
            # Drop non-speech before decoding so trailing silence in the
            # capture window doesn't get hallucinated into text. Threshold is
            # tunable: lower it to keep softer speech (FASTER_WHISPER_VAD_*).
            vad_filter=self._vad_filter,
            vad_parameters=(
                {"threshold": self._vad_threshold} if self._vad_filter else None
            ),
            # Each utterance is independent — don't carry context across
            # them, which otherwise feeds repetition loops ("1kg 1kg 1kg").
            condition_on_previous_text=False,
        )
        parts: list[str] = []
        probs: list[float] = []
        weights: list[int] = []
        nsp: list[float] = []
        for s in segments:
            txt = s.text.strip()
            if not txt:
                continue
            parts.append(txt)
            # avg_logprob is the mean per-token log-prob (≈ -1..0); exp() maps it
            # to a ~0..1 acoustic-model confidence. Weight by text length so a
            # long confident segment isn't diluted by a short shaky one.
            probs.append(math.exp(getattr(s, "avg_logprob", 0.0)))
            weights.append(len(txt))
            nsp.append(float(getattr(s, "no_speech_prob", 0.0)))
        text = " ".join(parts).strip()
        return TranscriptResult(text=text, confidence=_aggregate_confidence(probs, weights, nsp))

    async def transcribe(self, wav_path: Path) -> str:
        result = await self.transcribe_detailed(wav_path)
        return result.text

    async def transcribe_detailed(self, wav_path: Path) -> TranscriptResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._decode(wav_path))

    @classmethod
    def from_env(cls) -> FasterWhisperSTT:
        size = os.environ.get("FASTER_WHISPER_MODEL", "base").strip() or "base"
        # FASTER_WHISPER_LANGUAGE="" → auto-detect; unset → English default.
        lang = os.environ.get("FASTER_WHISPER_LANGUAGE", "en").strip() or None
        # FASTER_WHISPER_VAD_FILTER=0 disables the Silero pre-filter entirely;
        # FASTER_WHISPER_VAD_THRESHOLD lowers/raises its speech sensitivity
        # (default 0.5; try 0.2–0.3 if soft speech is being dropped).
        vad_filter = os.environ.get("FASTER_WHISPER_VAD_FILTER", "1").strip() not in (
            "0", "false", "no", "off", ""
        )
        try:
            vad_threshold = float(os.environ.get("FASTER_WHISPER_VAD_THRESHOLD", "0.5"))
        except ValueError:
            vad_threshold = 0.5
        return cls(
            model_size=size, language=lang,
            vad_filter=vad_filter, vad_threshold=vad_threshold,
        )


class GroqWhisperSTT(STT):
    """Groq Whisper API — cloud, requires ``GROQ_API_KEY``."""

    def __init__(self, api_key: str, model: str = "whisper-large-v3") -> None:
        self._api_key = api_key
        self._model = model

    async def transcribe(self, wav_path: Path) -> str:
        try:
            from groq import Groq  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "groq package not installed — run: uv pip install groq"
            ) from exc

        loop = asyncio.get_running_loop()

        def _run() -> str:
            client = Groq(api_key=self._api_key)
            with open(wav_path, "rb") as f:
                result = client.audio.transcriptions.create(
                    file=(wav_path.name, f.read()),
                    model=self._model,
                )
            return result.text.strip()

        return await loop.run_in_executor(None, _run)

    @classmethod
    def from_env(cls) -> GroqWhisperSTT:
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError("Set GROQ_API_KEY to use Groq STT")
        model = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3").strip()
        return cls(api_key=key, model=model)


def stt_from_env() -> STT:
    """Build an STT instance from the environment. Default: ``faster_whisper`` (local)."""
    provider = os.environ.get("STT_PROVIDER", "faster_whisper").strip().lower()
    if provider == "groq":
        return GroqWhisperSTT.from_env()
    return FasterWhisperSTT.from_env()
