"""Webcam capture subprocess.

Runs as ``python -m yuyutsava.events.sources._webcam_proc`` so it inherits
the parent venv but lives in its own process — cv2 + camera drivers are
heavy and crash-prone, and the macOS camera-permission grant is per-app,
not per-process, so isolation costs nothing in UX.

Protocol (line-delimited JSON over stdout):

    {"kind": "ready"}                                  on startup
    {"kind": "heartbeat", "ts": 1736...}               every 2s
    {"kind": "frame", "ts": ..., "blob_path": "...",
     "faces": [{"x":..,"y":..,"w":..,"h":..}],
     "width": 1280, "height": 720}                     on detection
    {"kind": "error", "msg": "..."}                    fatal — process exits

Stderr is free-form log text. The parent forwards both to its logger.

Tunables (CLI args):

    --blob-dir PATH         where to write JPEG frames
    --interval-ms N         minimum spacing between emitted frames (default 5000)
    --motion-threshold N    abs-diff sum threshold to consider "motion" (default 250000)
    --camera-index N        cv2 VideoCapture index (default 0)
    --jpeg-quality N        0..100 (default 80)
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

logger = logging.getLogger("yuyutsava.events.sources._webcam_proc")


def _emit(obj: dict) -> None:
    """Write one JSON line to stdout and flush. Parent reads line-by-line."""
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _fatal(msg: str, code: int = 1) -> None:
    _emit({"kind": "error", "msg": msg})
    sys.exit(code)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--blob-dir", required=True)
    p.add_argument("--interval-ms", type=int, default=5000)
    p.add_argument("--motion-threshold", type=int, default=250000)
    p.add_argument("--camera-index", type=int, default=0)
    p.add_argument("--jpeg-quality", type=int, default=80)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="webcam: %(message)s")

    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        _fatal("opencv-python / numpy missing; install yuyutsava[vision]")
        return

    blob_dir = Path(args.blob_dir).expanduser()
    blob_dir.mkdir(parents=True, exist_ok=True)

    # SIGTERM/SIGINT → clean exit. Parent sends SIGTERM on shutdown.
    stopping = {"v": False}

    def _stop(_sig, _frm):  # noqa: ANN001
        stopping["v"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    cap = cv2.VideoCapture(args.camera_index)
    if not cap.isOpened():
        _fatal(f"could not open camera index {args.camera_index}")
        return

    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(str(cascade_path))
    if cascade.empty():
        cap.release()
        _fatal(f"could not load Haar cascade at {cascade_path}")
        return

    _emit({"kind": "ready"})
    logger.info("opened camera %d, blob_dir=%s", args.camera_index, blob_dir)

    interval_sec = max(args.interval_ms, 500) / 1000.0
    motion_threshold = max(args.motion_threshold, 1000)
    jpeg_params = [int(cv2.IMWRITE_JPEG_QUALITY), max(0, min(100, args.jpeg_quality))]

    rolling_mean: "np.ndarray | None" = None
    last_emit_ts = 0.0
    last_heartbeat = 0.0

    try:
        while not stopping["v"]:
            now = time.time()
            if now - last_heartbeat >= 2.0:
                _emit({"kind": "heartbeat", "ts": now})
                last_heartbeat = now

            ok, frame = cap.read()
            if not ok or frame is None:
                # Transient camera hiccup — back off and retry.
                time.sleep(0.2)
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (0, 0), fx=0.25, fy=0.25)

            # Stage 1: motion vs rolling mean. Skip empty frames cheaply.
            small_f = small.astype("float32")
            if rolling_mean is None:
                rolling_mean = small_f.copy()
                time.sleep(0.05)
                continue
            diff = cv2.absdiff(small_f, rolling_mean)
            motion = float(diff.sum())
            # EMA the rolling mean — adapts to lighting drift but stays
            # reactive to fresh motion.
            rolling_mean = 0.9 * rolling_mean + 0.1 * small_f

            if motion < motion_threshold:
                time.sleep(0.1)
                continue

            # Honour the per-event spacing even when motion is constant
            # (e.g., user typing in front of the camera).
            if now - last_emit_ts < interval_sec:
                time.sleep(0.1)
                continue

            # Stage 2: confirm with Haar cascade. Cheap on grayscale.
            faces = cascade.detectMultiScale(
                gray, scaleFactor=1.2, minNeighbors=5, minSize=(60, 60)
            )
            if len(faces) == 0:
                time.sleep(0.1)
                continue

            ts_int = int(now * 1000)
            blob_path = blob_dir / f"webcam-{ts_int}.jpg"
            try:
                ok = cv2.imwrite(str(blob_path), frame, jpeg_params)
                if not ok:
                    logger.warning("imwrite returned False for %s", blob_path)
                    time.sleep(0.1)
                    continue
            except Exception as exc:
                logger.warning("imwrite failed: %s", exc)
                time.sleep(0.1)
                continue

            h, w = frame.shape[:2]
            _emit({
                "kind": "frame",
                "ts": now,
                "blob_path": str(blob_path),
                "faces": [
                    {"x": int(x), "y": int(y), "w": int(fw), "h": int(fh)}
                    for (x, y, fw, fh) in faces
                ],
                "width": int(w),
                "height": int(h),
            })
            last_emit_ts = now
    finally:
        try:
            cap.release()
        except Exception:
            pass
        logger.info("subprocess exiting (pid=%d)", os.getpid())


if __name__ == "__main__":
    main()
