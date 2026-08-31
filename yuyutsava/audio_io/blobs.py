"""On-disk storage for replayable voice audio (agent TTS clips).

A spoken turn's full TTS audio is buffered in memory while it streams to the
client (per-sentence), then persisted here as a single WAV so a *resumed* voice
session can replay it (Phase 6b). The DB row in ``voice_messages`` holds the
returned path; the bytes live under ``blobs/voice/<thread_id>/`` so deleting a
thread's directory drops all its clips.

Unlike the scratch blobs swept by :class:`yuyutsava.storage.sweeper.UnifiedSweeper`,
these are session-scoped user history with the session's lifetime — they are
removed when the session is deleted, not aged out by TTL.
"""

from __future__ import annotations

import logging
import uuid
import wave
from pathlib import Path

from yuyutsava.storage.paths import blobs_dir

logger = logging.getLogger("yuyutsava.audio_io.blobs")


def voice_blobs_dir() -> Path:
    """Root for persisted voice clips: ``blobs/voice/``."""
    return blobs_dir() / "voice"


def _thread_dir(thread_id: str) -> Path:
    # thread_ids are minted ``<role>-<ts>-<uuid>`` (filesystem-safe), but guard
    # against path separators just in case an external id ever flows through.
    safe = thread_id.replace("/", "_").replace("\\", "_")
    return voice_blobs_dir() / safe


def write_voice_wav(thread_id: str, pcm: bytes, sample_rate: int) -> str:
    """Write 16-bit mono PCM as a WAV under ``blobs/voice/<thread_id>/``.

    Returns the absolute path as a string (stored in ``voice_messages``).
    Synchronous stdlib I/O — callers on the event loop should wrap in
    ``asyncio.to_thread``.
    """
    d = _thread_dir(thread_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{uuid.uuid4().hex}.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm)
    return str(path)


def delete_thread_voice_blobs(thread_id: str) -> int:
    """Remove a thread's voice-clip directory. Returns files deleted."""
    d = _thread_dir(thread_id)
    if not d.exists():
        return 0
    removed = 0
    for f in d.glob("*.wav"):
        try:
            f.unlink(missing_ok=True)
            removed += 1
        except OSError:
            logger.debug("voice blobs: unlink %s failed", f, exc_info=True)
    try:
        d.rmdir()
    except OSError:
        pass  # non-empty or already gone — fine
    return removed
