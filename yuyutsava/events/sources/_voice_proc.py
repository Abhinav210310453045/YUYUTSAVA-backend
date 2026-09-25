"""Voice capture subprocess.

Runs as ``python -m yuyutsava.events.sources._voice_proc`` so it lives in
its own process — sounddevice and openwakeword use native audio drivers that
are hostile to the asyncio loop, and the subprocess isolation means a driver
crash never takes down the daemon.

Protocol (line-delimited JSON over stdout):

    {"kind": "ready"}
    {"kind": "heartbeat", "ts": 1736...}
    {"kind": "wake", "ts": ..., "wake_word": "hey_jarvis",
     "transcript": "", "blob_path": null, "duration_sec": 0.0}
    {"kind": "error", "msg": "..."}     # process exits after this

Stderr is free-form log text; the parent forwards it to its logger.

This process only DETECTS the wake word. The UI overlay's live mic and the
WS voice pipeline own capture and transcription, so a wake carries no
transcript or audio, and nothing is recorded here.

Tunables (CLI args / env vars):

    --blob-dir PATH          created if missing (kept for the parent's contract)
    --sample-rate N          mic sample rate in Hz (default 16000)
    --wake-words / WAKE_WORDS          comma-separated openwakeword model names
    --wake-threshold / WAKE_THRESHOLD  float 0..1 detection threshold (default 0.5)
    --capture-sec, --stt-provider      accepted from the parent, unused
    YUYUTSAVA_VOICE_LOG_LEVEL          e.g. DEBUG for the live wake-score readout
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("yuyutsava.events.sources._voice_proc")

_CHUNK_FRAMES = 1280  # ~80ms at 16kHz — openwakeword's native chunk size
_HEARTBEAT_SEC = 2.0


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _fatal(msg: str, code: int = 1) -> None:
    _emit({"kind": "error", "msg": msg})
    sys.exit(code)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--blob-dir", required=True)
    p.add_argument("--capture-sec", type=float, default=8.0)
    p.add_argument("--stt-provider", default="faster_whisper")
    p.add_argument("--sample-rate", type=int, default=16000)
    # Wake config may arrive via events_config params (lets the Settings UI /
    # onboarding hot-apply a new wake word) or fall back to WAKE_WORDS env.
    p.add_argument(
        "--wake-words",
        default=None,
        help="comma-separated openwakeword model names (overrides WAKE_WORDS)",
    )
    p.add_argument(
        "--wake-threshold", default=None, help="detection threshold 0..1 (overrides WAKE_THRESHOLD)"
    )
    return p.parse_args(argv)


def _configure(args: argparse.Namespace) -> None:
    """Apply wake params over env, and set this subprocess's log level.

    Params take precedence over env so a hot-reloaded events config wins. The
    parent forwards our stderr to its own logger, so these lines appear in the
    daemon log at the level set by ``YUYUTSAVA_VOICE_LOG_LEVEL`` (default INFO).
    """
    if args.wake_words:
        os.environ["WAKE_WORDS"] = args.wake_words
    if args.wake_threshold:
        os.environ["WAKE_THRESHOLD"] = args.wake_threshold
    level_name = os.environ.get("YUYUTSAVA_VOICE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, stream=sys.stderr, format="voice_proc: %(message)s")


def _listen(sd: Any, detector: Any, sample_rate: int, stopping: dict) -> None:
    """Read the mic in chunks and emit a wake the moment one fires.

    Heartbeats go out every few seconds. The wake is emitted IMMEDIATELY so the
    UI overlay pops with sub-second latency, then this goes straight back to
    listening. Five detector failures in a row is a broken model, not a
    glitch — it would repeat every ~80ms chunk — so the process gives up.
    """
    last_heartbeat = time.time()
    with sd.RawInputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
        blocksize=_CHUNK_FRAMES,
    ) as stream:
        detector_errors = 0  # consecutive process() failures
        while not stopping["v"]:
            now = time.time()
            if now - last_heartbeat >= _HEARTBEAT_SEC:
                _emit({"kind": "heartbeat", "ts": now})
                last_heartbeat = now

            raw, _ = stream.read(_CHUNK_FRAMES)
            try:
                fired = detector.process(bytes(raw))
                detector_errors = 0
            except Exception as exc:
                detector_errors += 1
                if detector_errors >= 5:
                    _fatal(f"wake detector unusable, giving up: {exc}")
                    break
                logger.warning("wake detector error: %s", exc)
                continue
            if fired is None:
                continue

            logger.info("wake word '%s' detected; popping overlay", fired)
            detector.reset()
            _emit(
                {
                    "kind": "wake",
                    "ts": time.time(),
                    "wake_word": fired,
                    "transcript": "",
                    "blob_path": None,
                    "duration_sec": 0.0,
                }
            )
            last_heartbeat = time.time()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    _configure(args)

    # Check deps before anything else.
    try:
        import numpy as np  # type: ignore  # noqa: F401, PLC0415 — optional dependency, checked here
        import sounddevice as sd  # type: ignore  # noqa: PLC0415 — optional dependency
    except ImportError:
        _fatal("sounddevice / numpy missing — run: uv pip install 'yuyutsava[voice]'")
        return

    try:
        # Subprocess entry module.
        from yuyutsava.voice.backends.wake import wake_from_env  # noqa: PLC0415
    except Exception as exc:
        _fatal(f"Failed to import wake module: {exc}")
        return

    Path(args.blob_dir).expanduser().mkdir(parents=True, exist_ok=True)

    # Load wake detector (lazy — first process() call loads the model).
    try:
        detector = wake_from_env()
    except Exception as exc:
        _fatal(f"Failed to build wake detector: {exc}")
        return

    # SIGTERM / SIGINT → clean exit. Cross-platform: registers SIGINT + (SIGTERM
    # on POSIX / SIGBREAK on Windows); a bare signal.signal(SIGTERM, …) raises
    # ValueError on Windows.
    stopping = {"v": False}

    def _stop() -> None:
        stopping["v"] = True

    # Subprocess entry module.
    from yuyutsava.platform import install_terminate_handler  # noqa: PLC0415

    install_terminate_handler(_stop)

    _emit({"kind": "ready"})
    logger.info("voice source ready — sample_rate=%d (wake-detection only)", args.sample_rate)
    try:
        _listen(sd, detector, args.sample_rate, stopping)
    except Exception as exc:
        _fatal(f"audio stream error: {exc}")

    logger.info("subprocess exiting (pid=%d)", os.getpid())


if __name__ == "__main__":
    main()
