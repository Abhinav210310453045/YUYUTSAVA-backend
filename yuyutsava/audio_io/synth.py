"""Synthesize text to raw PCM for streaming to a remote client.

The daemon-local :class:`~yuyutsava.audio_io.announcer.Announcer` *plays* audio on
the host's speakers. The WS voice path is different: the client (Electron/mobile)
may be remote, so the daemon must turn agent text into **PCM bytes** and stream
them back as ``audio_chunk`` frames for the renderer's Web Audio queue to play.

:func:`synthesize_pcm` runs the configured TTS backend to a temp file and decodes
it to 16-bit mono PCM. Piper emits WAV (decoded with the stdlib ``wave`` module);
other backends (e.g. ElevenLabs MP3) are decoded via ``soundfile`` when present.
"""

from __future__ import annotations

import logging
import tempfile
import wave
from pathlib import Path

from yuyutsava.io.tts import TTS

logger = logging.getLogger("yuyutsava.audio_io.synth")


async def synthesize_pcm(tts: TTS, text: str) -> tuple[bytes, int]:
    """Synthesize ``text`` and return ``(pcm_int16_le_bytes, sample_rate)``.

    Mono, 16-bit little-endian. Returns ``(b"", 0)`` for empty text. Raises if
    synthesis itself fails (caller decides whether to degrade).
    """
    text = (text or "").strip()
    if not text:
        return b"", 0
    with tempfile.TemporaryDirectory(prefix="yuyutsava_synth_") as td:
        out = Path(td) / "tts.wav"
        await tts.synthesize(text, out)
        return _decode_to_pcm(out)


def _decode_to_pcm(path: Path) -> tuple[bytes, int]:
    """Decode an audio file to mono int16 PCM bytes + sample rate."""
    # Fast path: a real WAV (piper) decodes with the stdlib, no extra deps.
    try:
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate()
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        if width == 2 and channels == 1:
            return raw, rate
        return _to_mono16(raw, channels, width), rate
    except wave.Error:
        pass  # not a PCM WAV — try soundfile (mp3/flac/ogg)

    try:
        import numpy as np  # type: ignore
        import soundfile as sf  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "non-WAV TTS output needs soundfile — run: uv pip install 'yuyutsava[voice]'"
        ) from exc

    data, rate = sf.read(str(path), dtype="int16", always_2d=False)
    if getattr(data, "ndim", 1) == 2:  # stereo -> mono
        data = data.mean(axis=1).astype(np.int16)
    return data.tobytes(), int(rate)


def _to_mono16(raw: bytes, channels: int, width: int) -> bytes:
    """Best-effort downmix/normalize odd WAV formats to mono int16."""
    import audioop  # stdlib

    if width != 2:
        raw = audioop.lin2lin(raw, width, 2)
        width = 2
    if channels > 1:
        raw = audioop.tomono(raw, width, 0.5, 0.5)
    return raw
