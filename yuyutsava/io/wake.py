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

# Default openwakeword model. The ONNX weights are fetched on first use (see
# OpenWakeWordDetector._load), not bundled with the package.
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
        self._last_score_log = 0.0       # throttle for the live score readout
        self._score_log_every = 2.0      # seconds between readouts
        self._peak_since_log = 0.0       # loudest match seen since last readout
        self._peak_word = ""

    def _valid_wake_words(self, official: set[str]) -> list[str]:
        """Keep only wake words openwakeword can actually load.

        openwakeword recognises a fixed set of pretrained models; any other name
        is treated as a path to a custom-trained ``.onnx`` file. An arbitrary
        word like "Yuyutsava" is neither — it would make onnxruntime fail with
        INVALID_PROTOBUF on every chunk. Drop unknown words (with a clear
        warning) and fall back to the default model if nothing valid remains.
        """
        valid, unknown = [], []
        for w in self._wake_words:
            # An official model name, or a path to a real custom .onnx file.
            # Require isfile()+.onnx (not just exists()) so a stray name doesn't
            # accidentally match a directory — e.g. the case-insensitive macOS FS
            # makes "Yuyutsava" match the yuyutsava/ package dir.
            if w in official or (w.endswith(".onnx") and os.path.isfile(w)):
                valid.append(w)
            else:
                unknown.append(w)
        if unknown:
            logger.warning(
                "ignoring unsupported wake word(s) %s — openwakeword only ships "
                "models for %s; a custom word needs a trained .onnx model "
                "(point WAKE_WORDS at its path)",
                unknown, sorted(official),
            )
        if not valid:
            logger.warning("no usable wake words configured; falling back to %s",
                           DEFAULT_WAKE_WORDS)
            valid = list(DEFAULT_WAKE_WORDS)
        return valid

    def _load(self):
        if self._model is None:
            try:
                import openwakeword  # type: ignore
                from openwakeword.model import Model  # type: ignore
                from openwakeword import utils as oww_utils  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "openwakeword not installed — run: uv pip install 'yuyutsava[voice]'"
                ) from exc
            # Validate against the official model catalog before we try to load
            # anything, so a bad wake word degrades to the default instead of
            # crash-looping the voice subprocess.
            self._wake_words = self._valid_wake_words(set(openwakeword.MODELS.keys()))
            # The pretrained ONNX models are NOT bundled with the package — they
            # must be fetched on first use. Without them ``Model()`` raises
            # "Could not find pretrained model" on every call, flooding the log.
            # download_models() is idempotent: it skips files already present, so
            # this is a no-op once the models exist.
            try:
                oww_utils.download_models(self._wake_words)
            except Exception as exc:  # network down, etc. — surface clearly
                raise RuntimeError(
                    f"openwakeword pretrained models missing and download failed: {exc}. "
                    f"Connect to the internet once, or pre-fetch with "
                    f"python -c \"import openwakeword.utils as u; u.download_models()\""
                ) from exc
            self._model = Model(
                wakeword_models=self._wake_words,
                inference_framework="onnx",
            )
        return self._model

    def process(self, chunk: bytes) -> str | None:
        import time

        import numpy as np  # type: ignore

        model = self._load()
        # openwakeword expects int16 numpy arrays at 16kHz.
        audio = np.frombuffer(chunk, dtype=np.int16)
        scores = model.predict(audio)

        # Track the loudest match so the periodic readout below reflects the peak
        # of the window, not whatever the last 80 ms chunk happened to be.
        top_word, top_score = "", 0.0
        for word, score in scores.items():
            if score > top_score:
                top_word, top_score = word, float(score)
        if top_score > self._peak_since_log:
            self._peak_since_log, self._peak_word = top_score, top_word

        # Live diagnostic: every couple of seconds, log the peak wake score so a
        # user can SEE whether the mic is being heard at all (score moves when you
        # speak) and how close they are to firing (tune WAKE_THRESHOLD). A flat
        # ~0.000 while speaking almost always means the process has no mic audio
        # (grant Terminal/the app Microphone permission in System Settings).
        now = time.time()
        if now - self._last_score_log >= self._score_log_every:
            self._last_score_log = now
            logger.debug(
                "wake: peak score %.3f (%s) — fires at >= %.2f",
                self._peak_since_log, self._peak_word or "?", self._threshold,
            )
            self._peak_since_log, self._peak_word = 0.0, ""

        if top_word and top_score >= self._threshold:
            logger.info("wake word '%s' fired (score=%.3f)", top_word, top_score)
            return top_word
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
