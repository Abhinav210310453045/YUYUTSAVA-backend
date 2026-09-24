"""Writing 16-bit PCM to a WAV file.

Five places wrote the same five lines — the voice blob store, the converse
pipeline, the earcon generator, the microphone backend and the TODO board's
audio block (`R-DRY-02`, W4). The format is always 16-bit little-endian PCM;
what varies is the rate, and that is why it is a required argument here with no
default.

A default would be a trap. Speech is captured and processed at 16 kHz, but a
TTS engine writes at its own rate, and audio written at the wrong rate plays
back at the wrong pitch and speed without any error at all. Making every caller
name the rate is what stops the obvious default from being silently wrong.

A leaf beside :mod:`yuyutsava.ansi` and :mod:`yuyutsava.text`, because the
storage layer and the board both write WAVs and neither may import ``voice``.
"""

from __future__ import annotations

import wave
from pathlib import Path

#: Bytes per sample. 16-bit is the only width anything here produces or reads.
SAMPLE_WIDTH = 2


def write_pcm16(path: str | Path, pcm: bytes, *, sample_rate: int, channels: int = 1) -> Path:
    """Write *pcm* as a 16-bit WAV at *sample_rate*, creating the directory.

    Synchronous stdlib I/O — callers on the event loop should wrap it in
    ``asyncio.to_thread``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm)
    return path


__all__ = ["SAMPLE_WIDTH", "write_pcm16"]
