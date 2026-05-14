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
import os
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger("yuyutsava.io.stt")


class STT(ABC):
    """Speech-to-text ABC. Implementations return a stripped transcript string."""

    @abstractmethod
    async def transcribe(self, wav_path: Path) -> str: ...


class FasterWhisperSTT(STT):
    """Local transcription via ``faster-whisper``. No network or API key needed."""

    def __init__(self, model_size: str = "base") -> None:
        self._model_size = model_size
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

    async def transcribe(self, wav_path: Path) -> str:
        loop = asyncio.get_running_loop()

        def _run() -> str:
            model = self._load()
            segments, _ = model.transcribe(str(wav_path), beam_size=5)
            return " ".join(s.text.strip() for s in segments).strip()

        return await loop.run_in_executor(None, _run)

    @classmethod
    def from_env(cls) -> FasterWhisperSTT:
        size = os.environ.get("FASTER_WHISPER_MODEL", "base").strip() or "base"
        return cls(model_size=size)


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
