"""PortAudio capture/playback via sounddevice + soundfile.

All audio I/O goes through this module so VoiceChannel never imports
sounddevice or soundfile directly.  Raises ``AudioUnavailableError`` when
the optional voice extras are not installed.

Install: uv pip install 'yuyutsava[voice]'
"""

from __future__ import annotations

import asyncio
import logging
import wave
from pathlib import Path

from yuyutsava.voice.pcm import CHANNELS, SAMPLE_RATE
from yuyutsava.wav import write_pcm16

logger = logging.getLogger("yuyutsava.voice.backends.device")


class AudioUnavailableError(RuntimeError):
    """Raised when sounddevice / soundfile / numpy are not installed."""


def _require_sd():
    try:
        import sounddevice as sd  # type: ignore  # noqa: PLC0415 — optional dependency

        return sd
    except ImportError as exc:
        raise AudioUnavailableError(
            "sounddevice not installed — run: uv pip install 'yuyutsava[voice]'"
        ) from exc


def _require_np():
    try:
        import numpy as np  # type: ignore  # noqa: PLC0415 — optional dependency

        return np
    except ImportError as exc:
        raise AudioUnavailableError("numpy not installed") from exc


def _require_sf():
    try:
        import soundfile as sf  # type: ignore  # noqa: PLC0415 — optional dependency

        return sf
    except ImportError as exc:
        raise AudioUnavailableError(
            "soundfile not installed — run: uv pip install 'yuyutsava[voice]'"
        ) from exc


async def capture_wav(path: Path, duration_sec: float, sample_rate: int = SAMPLE_RATE) -> None:
    """Record ``duration_sec`` seconds from the default mic and write a WAV to ``path``."""
    sd = _require_sd()
    _require_np()
    loop = asyncio.get_running_loop()

    def _record() -> bytes:
        data = sd.rec(
            int(duration_sec * sample_rate),
            samplerate=sample_rate,
            channels=CHANNELS,
            dtype="int16",
            blocking=True,
        )
        return data.tobytes()

    raw = await loop.run_in_executor(None, _record)
    write_pcm16(path, raw, sample_rate=sample_rate, channels=CHANNELS)


async def play_wav(path: Path) -> None:
    """Play a WAV file without requiring soundfile (stdlib wave only)."""
    sd = _require_sd()
    np = _require_np()
    loop = asyncio.get_running_loop()

    def _play() -> None:
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        data = np.frombuffer(raw, dtype=np.int16)
        sd.play(data, rate)
        sd.wait()

    await loop.run_in_executor(None, _play)


def stop_playback() -> None:
    """Immediately stop any audio currently playing on the output device.

    Synchronous and cheap (PortAudio ``sd.stop()``); used for barge-in so a new
    utterance / earcon can cut off the current one. No-op when audio is
    unavailable.
    """
    try:
        sd = _require_sd()
    except AudioUnavailableError:
        return
    sd.stop()
