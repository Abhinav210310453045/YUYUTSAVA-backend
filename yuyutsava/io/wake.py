"""Wake-word detection backends.

Default: ``openwakeword`` (local, model downloaded on first use).
Extend by subclassing ``WakeWordDetector`` and registering via ``wake_from_env``.

This module is used **inside the ``_voice_proc`` subprocess only** — the
parent daemon never imports it directly.

Install: uv pip install 'yuyutsava[voice]'
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger("yuyutsava.io.wake")

# Default wake words bundled with openwakeword — no download required.
DEFAULT_WAKE_WORDS = ["hey_jarvis"]


class WakeWordDetector(ABC):
    """ABC for synchronous streaming wake-word detectors.

    ``process(chunk)`` is called on each PCM-int16 chunk (16 kHz mono).
    Returns the detected wake-word name if fired, else ``None``.
    """

    @abstractmethod
    def process(self, chunk: bytes) -> str | None:
        """Feed one audio chunk; return wake-word label on detection, else None."""

    @abstractmethod
    def reset(self) -> None:
        """Reset internal state between utterances."""


class OpenWakeWordDetector(WakeWordDetector):
    """openwakeword-backed detector.

    ``wake_words`` defaults to the built-in "hey_jarvis" model.
    ``threshold`` (0.0–1.0) controls sensitivity; 0.5 is a good start.
    """

    def __init__(
        self,
        wake_words: list[str] | None = None,
        threshold: float = 0.5,
        chunk_size: int = 1280,
    ) -> None:
        self._wake_words = wake_words or DEFAULT_WAKE_WORDS
        self._threshold = threshold
        self._chunk_size = chunk_size
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from openwakeword.model import Model  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "openwakeword not installed — run: uv pip install 'yuyutsava[voice]'"
                ) from exc
            self._model = Model(
                wakeword_models=self._wake_words,
                inference_framework="onnx",
            )
        return self._model

    def process(self, chunk: bytes) -> str | None:
        import numpy as np  # type: ignore

        model = self._load()
        # openwakeword expects int16 numpy arrays at 16kHz.
        audio = np.frombuffer(chunk, dtype=np.int16)
        scores = model.predict(audio)
        for word, score in scores.items():
            if score >= self._threshold:
                logger.debug("wake word '%s' score=%.3f", word, score)
                return word
        return None

    def reset(self) -> None:
        # openwakeword models have no explicit reset; re-instantiating is the
        # safe path for clearing accumulated state.
        self._model = None

    @classmethod
    def from_env(cls) -> OpenWakeWordDetector:
        raw = os.environ.get("WAKE_WORDS", "").strip()
        words = [w.strip() for w in raw.split(",") if w.strip()] if raw else None
        threshold = float(os.environ.get("WAKE_THRESHOLD", "0.5"))
        return cls(wake_words=words, threshold=threshold)


def wake_from_env() -> WakeWordDetector:
    """Build a WakeWordDetector from the environment. Default: ``openwakeword``."""
    provider = os.environ.get("WAKE_PROVIDER", "openwakeword").strip().lower()
    if provider == "openwakeword":
        return OpenWakeWordDetector.from_env()
    raise RuntimeError(f"Unknown WAKE_PROVIDER={provider!r}; only 'openwakeword' is supported")
