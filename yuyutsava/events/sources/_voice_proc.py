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


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--blob-dir", required=True)
    p.add_argument("--capture-sec", type=float, default=8.0)
    p.add_argument("--stt-provider", default="faster_whisper")
    p.add_argument("--sample-rate", type=int, default=16000)
    # Wake config may arrive via events_config params (lets the Settings UI /
    # onboarding hot-apply a new wake word) or fall back to WAKE_WORDS env.
    p.add_argument("--wake-words", default=None,
                   help="comma-separated openwakeword model names (overrides WAKE_WORDS)")
    p.add_argument("--wake-threshold", default=None,
                   help="detection threshold 0..1 (overrides WAKE_THRESHOLD)")
    args = p.parse_args(argv)

    # Params take precedence over env so a hot-reloaded events config wins.
    if args.wake_words:
        os.environ["WAKE_WORDS"] = args.wake_words
    if args.wake_threshold:
        os.environ["WAKE_THRESHOLD"] = args.wake_threshold

    # Log level for this subprocess. Set YUYUTSAVA_VOICE_LOG_LEVEL=DEBUG in the
    # daemon env (e.g. .env) to surface the live wake-score readout and other
    # DEBUG diagnostics; defaults to INFO. The parent forwards our stderr to its
    # own logger, so these lines appear in the daemon log either way.
    _level_name = os.environ.get("YUYUTSAVA_VOICE_LOG_LEVEL", "INFO").upper()
    _level = getattr(logging, _level_name, logging.INFO)
    logging.basicConfig(level=_level, stream=sys.stderr,
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

    # Load wake detector (lazy — first process() call loads the model).
    try:
        detector = wake_from_env()
    except Exception as exc:
        _fatal(f"Failed to build wake detector: {exc}")
        return

    # NOTE: no STT is loaded here. This subprocess only DETECTS the wake word and
    # emits an instant signal; the UI overlay's live mic + the WS voice pipeline
    # own all capture/transcription. (Loading a second whisper here starved the
    # pipeline's STT of CPU — the cause of the 10–20s transcription lag.)

    # SIGTERM / SIGINT → clean exit.
    stopping = {"v": False}

    def _stop(_sig, _frm):  # noqa: ANN001
        stopping["v"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    _emit({"kind": "ready"})
    logger.info("voice source ready — sample_rate=%d (wake-detection only)", sample_rate)

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
            detector_errors = 0  # consecutive process() failures
            while not stopping["v"]:
                now = time.time()
                if now - last_heartbeat >= _HEARTBEAT_SEC:
                    _emit({"kind": "heartbeat", "ts": now})
                    last_heartbeat = now

                raw, _ = stream.read(_CHUNK_FRAMES)
                chunk = bytes(raw)

                try:
                    fired = detector.process(chunk)
                    detector_errors = 0
                except Exception as exc:
                    # A failure here is almost always a broken/missing model, not
                    # a transient audio glitch — and it would repeat every chunk
                    # (~80ms), flooding the log. Give up after a few in a row.
                    detector_errors += 1
                    if detector_errors >= 5:
                        _fatal(f"wake detector unusable, giving up: {exc}")
                        break
                    logger.warning("wake detector error: %s", exc)
                    continue

                if fired is None:
                    continue

                # ── Wake fired! ──────────────────────────────────────────────
                # Emit the wake signal IMMEDIATELY so the UI overlay pops with
                # sub-second latency, then go straight back to listening for the
                # next wake word. This subprocess does NOT capture or transcribe
                # the utterance: the UI overlay opens its own live mic the instant
                # it pops and (with the WS voice pipeline) owns the whole
                # conversation, including a same-breath command. The old code
                # captured a fixed 8s window and ran STT here first — seconds of
                # lag, a transcript the overlay discarded, AND a second whisper
                # that starved the pipeline's STT of CPU (the 10–20s lag).
                logger.info("wake word '%s' detected; popping overlay", fired)
                detector.reset()
                _emit({
                    "kind": "wake",
                    "ts": time.time(),
                    "wake_word": fired,
                    "transcript": "",
                    "blob_path": None,
                    "duration_sec": 0.0,
                })
                last_heartbeat = time.time()

    except Exception as exc:
        _fatal(f"audio stream error: {exc}")

    logger.info("subprocess exiting (pid=%d)", os.getpid())


if __name__ == "__main__":
    main()
