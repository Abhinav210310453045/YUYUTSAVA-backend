"""Audio artifact block: audio files on cards + TTS-generated voice notes.

Phase-7 proof of the pluggability contract (docs/design/todo-board.md §8): the
whole backend of the block is this module plus one ``register_block`` entry
in ``artifacts.py`` — zero edits to exchange/store/router/tools. Rows ride
the closed V1 kind vocabulary as ``kind="file"`` refined by ``audio/*``
mimes, exactly the umbrella the Phase-4 checks proved.

``generate(spec, out_dir)`` delegates to the voice interface's existing synth
path (:func:`yuyutsava.audio_io.synth.synthesize_pcm` over
:func:`yuyutsava.io.tts.tts_from_env`: Piper when ``PIPER_MODEL`` is set,
zero-config macOS ``say``/pyttsx3 fallback otherwise) and writes a mono
16-bit WAV into the card workspace for the caller to ``attach()``.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import Any

from yuyutsava.todoboard.artifacts import ArtifactBlock, _file_validator
from yuyutsava.todoboard.exchange import TodoValidationError

# What the multipart endpoint accepts. Rendering is broader — the frontend
# block claims any audio/* mime and plays it through Web Audio decoding.
_UPLOAD_MIMES = (
    "audio/wav", "audio/x-wav", "audio/wave",
    "audio/mpeg", "audio/mp4", "audio/aac",
    "audio/ogg", "audio/webm", "audio/flac",
)


def _generate_audio(spec: dict[str, Any], out_dir: Path) -> tuple[Path, str]:
    """Speak ``spec["text"]`` into a WAV under *out_dir* via the TTS pipeline.

    Synchronous like every generator (callers run generate off the event
    loop, mirroring how validators are dispatched); the async synth path is
    driven with ``asyncio.run``.
    """
    from ulid import ULID

    from yuyutsava.audio_io.synth import synthesize_pcm
    from yuyutsava.io.tts import tts_from_env

    text = str(spec.get("text") or "").strip()
    if not text:
        raise TodoValidationError('audio generation needs spec["text"] to speak')

    pcm, rate = asyncio.run(synthesize_pcm(tts_from_env(), text))
    if not pcm:
        raise TodoValidationError("TTS produced no audio for the given text")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"tts_{ULID()}.wav"
    with wave.open(str(path), "wb") as wf:  # write_voice_wav pattern
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(int(rate))
        wf.writeframes(pcm)
    return path, "audio/wav"


AUDIO_BLOCK = ArtifactBlock(
    name="audio", kind="file",  # closed V1 vocabulary: audio rides "file" by mime
    validate=_file_validator("file", family="audio"),
    mimes=("audio/*",),
    upload_mimes=_UPLOAD_MIMES,
    generate=_generate_audio,
)

__all__ = ["AUDIO_BLOCK"]
