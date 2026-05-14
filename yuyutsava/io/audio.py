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

logger = logging.getLogger("yuyutsava.io.audio")

SAMPLE_RATE = 16_000
CHANNELS = 1


class AudioUnavailableError(RuntimeError):
    """Raised when sounddevice / soundfile / numpy are not installed."""


def _require_sd():
    try:
        import sounddevice as sd  # type: ignore
        return sd
    except ImportError as exc:
        raise AudioUnavailableError(
            "sounddevice not installed — run: uv pip install 'yuyutsava[voice]'"
        ) from exc


def _require_np():
    try:
        import numpy as np  # type: ignore
        return np
    except ImportError as exc:
        raise AudioUnavailableError("numpy not installed") from exc


def _require_sf():
    try:
        import soundfile as sf  # type: ignore
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(raw)


async def play_audio(path: Path) -> None:
    """Play an audio file (WAV or MP3) through the default output device.

    Uses soundfile for decoding so MP3, FLAC, OGG, and WAV all work.
    """
    sd = _require_sd()
    sf = _require_sf()
    loop = asyncio.get_running_loop()

    def _play() -> None:
        data, rate = sf.read(str(path), dtype="int16", always_2d=False)
        sd.play(data, rate)
        sd.wait()

    await loop.run_in_executor(None, _play)


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
