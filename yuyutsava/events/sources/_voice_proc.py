"""Voice capture subprocess.

Runs as ``python -m yuyutsava.events.sources._voice_proc`` so it lives in
its own process — sounddevice and openwakeword use native audio drivers that
are hostile to the asyncio loop, and the subprocess isolation means a driver
crash never takes down the daemon.

Protocol (line-delimited JSON over stdout):

    {"kind": "ready"}
    {"kind": "heartbeat", "ts": 1736...}
    {"kind": "wake", "ts": ..., "blob_path": "...", "transcript": "...",
     "wake_word": "hey_jarvis", "duration_sec": 8.2}
    {"kind": "error", "msg": "..."}     # process exits after this

Stderr is free-form log text; the parent forwards it to its logger.

Tunables (CLI args / env vars):

    --blob-dir PATH          where to write .wav utterance files
    --capture-sec N          seconds of audio to capture after wake (default 8)
    --stt-provider NAME      "faster_whisper" | "groq" | "none" (default: faster_whisper)
    --sample-rate N          mic sample rate in Hz (default 16000)
    WAKE_WORDS               comma-separated openwakeword model names
    WAKE_THRESHOLD           float 0..1 detection threshold (default 0.5)
    STT_PROVIDER             overrides --stt-provider
    FASTER_WHISPER_MODEL     model size for faster_whisper (default "base")
    GROQ_API_KEY / GROQ_WHISPER_MODEL  for Groq STT

Privacy: captured WAV blobs land in blob_dir and are TTL-swept by the
BlobSweeper (default 1h). Nothing leaves the device unless STT_PROVIDER=groq.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

logger = logging.getLogger("yuyutsava.events.sources._voice_proc")

_CHUNK_FRAMES = 1280  # ~80ms at 16kHz — openwakeword's native chunk size
_HEARTBEAT_SEC = 2.0


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _fatal(msg: str, code: int = 1) -> None:
    _emit({"kind": "error", "msg": msg})
    sys.exit(code)


def _build_stt(provider: str):
    """Return an STT instance or None if provider == 'none'."""
    if provider == "none":
        return None
    # Import here — subprocess inherits the parent venv.
    from yuyutsava.io.stt import stt_from_env
    os.environ.setdefault("STT_PROVIDER", provider)
    try:
        return stt_from_env()
    except Exception as exc:
        logger.warning("STT setup failed (%s): %s — transcripts will be empty", provider, exc)
        return None


def _transcribe_sync(stt, wav_path: Path) -> str:
    """Run STT synchronously (we're in a subprocess, no event loop)."""
    if stt is None:
        return ""
    try:
        import asyncio
        return asyncio.run(stt.transcribe(wav_path))
    except Exception as exc:
        logger.warning("STT transcription failed: %s", exc)
        return ""


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--blob-dir", required=True)
    p.add_argument("--capture-sec", type=float, default=8.0)
    p.add_argument("--stt-provider", default="faster_whisper")
    p.add_argument("--sample-rate", type=int, default=16000)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="voice_proc: %(message)s")

    # Check deps before anything else.
    try:
        import sounddevice as sd  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        _fatal("sounddevice / numpy missing — run: uv pip install 'yuyutsava[voice]'")
        return

    try:
        from yuyutsava.io.wake import wake_from_env
    except Exception as exc:
        _fatal(f"Failed to import wake module: {exc}")
        return

    blob_dir = Path(args.blob_dir).expanduser()
    blob_dir.mkdir(parents=True, exist_ok=True)
    sample_rate = args.sample_rate
    capture_frames = int(args.capture_sec * sample_rate)

    # Load wake detector (lazy — first process() call loads the model).
    try:
        detector = wake_from_env()
    except Exception as exc:
        _fatal(f"Failed to build wake detector: {exc}")
        return

    # Load STT (may be None if provider == "none").
    stt = _build_stt(args.stt_provider)

    # SIGTERM / SIGINT → clean exit.
    stopping = {"v": False}

    def _stop(_sig, _frm):  # noqa: ANN001
        stopping["v"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    _emit({"kind": "ready"})
    logger.info(
        "voice source ready — sample_rate=%d capture_sec=%.1f stt=%s",
        sample_rate, args.capture_sec, args.stt_provider,
    )

    last_heartbeat = time.time()

    # Open a blocking InputStream; iterate chunks in the main thread.
    try:
        with sd.RawInputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            blocksize=_CHUNK_FRAMES,
        ) as stream:
            chunk_buf = b""
            while not stopping["v"]:
                now = time.time()
                if now - last_heartbeat >= _HEARTBEAT_SEC:
                    _emit({"kind": "heartbeat", "ts": now})
                    last_heartbeat = now

                raw, _ = stream.read(_CHUNK_FRAMES)
                chunk = bytes(raw)

                try:
                    fired = detector.process(chunk)
                except Exception as exc:
                    logger.warning("wake detector error: %s", exc)
                    continue

                if fired is None:
                    continue

                # ── Wake fired! Capture utterance ────────────────────────────
                logger.info("wake word '%s' detected; capturing utterance", fired)
                detector.reset()

                ts_int = int(time.time() * 1000)
                wav_path = blob_dir / f"voice-{ts_int}.wav"

                try:
                    utterance_raw, _ = stream.read(capture_frames)
                    utterance_bytes = bytes(utterance_raw)
                except Exception as exc:
                    logger.warning("utterance capture failed: %s", exc)
                    continue

                # Write WAV.
                try:
                    import wave
                    with wave.open(str(wav_path), "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)  # int16
                        wf.setframerate(sample_rate)
                        wf.writeframes(utterance_bytes)
                except Exception as exc:
                    logger.warning("failed to write WAV: %s", exc)
                    continue

                duration_sec = len(utterance_bytes) / (2 * sample_rate)
                transcript = _transcribe_sync(stt, wav_path)

                _emit({
                    "kind": "wake",
                    "ts": time.time(),
                    "blob_path": str(wav_path),
                    "transcript": transcript,
                    "wake_word": fired,
                    "duration_sec": duration_sec,
                })

                # Emit a heartbeat immediately after so the parent's watchdog
                # doesn't count the STT latency as a missed heartbeat.
                _emit({"kind": "heartbeat", "ts": time.time()})
                last_heartbeat = time.time()

    except Exception as exc:
        _fatal(f"audio stream error: {exc}")

    logger.info("subprocess exiting (pid=%d)", os.getpid())


if __name__ == "__main__":
    main()
