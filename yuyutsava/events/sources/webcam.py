"""Webcam event source — parent half.

Spawns ``python -m yuyutsava.events.sources._webcam_proc`` as a subprocess and
bridges its line-delimited JSON output into ``face.frame`` events on the bus.

Why a subprocess:
  - cv2 + native camera drivers can crash; isolation keeps the daemon up.
  - macOS asks for camera permission once per parent app; the subprocess
    inherits that grant.
  - The orchestrator's asyncio loop should never block on a USB driver.

Heartbeat watchdog: the child writes ``{"kind":"heartbeat"}`` every ~2s. If
no heartbeat (or any other line) arrives for ``heartbeat_timeout_sec``
(default 8s = 3 missed beats + slack), the parent SIGTERMs the child and
the registry's exponential backoff respawns this source.

Config (params from ``events_config.json``)::

    {
      "enabled": false,                 // disabled by default — privacy
      "blob_dir": "~/.yuyutsava/blobs/webcam", // jpeg frames land here (TTL-swept)
      "interval_ms": 5000,              // min spacing between emitted frames
      "motion_threshold": 250000,
      "camera_index": 0,
      "jpeg_quality": 80,
      "heartbeat_timeout_sec": 8
    }

Frames go to disk; only the path travels through the bus envelope. The
:class:`yuyutsava.storage.sweeper.UnifiedSweeper` deletes stale JPEGs
(default TTL ~1h) and the matching ``event_payloads.blob_path`` rows.
The deepface enrolled-faces DB at ``~/.yuyutsava/deepface/`` is in a
separate directory and is never touched by this sweep.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from yuyutsava.events.registry import register_source
from yuyutsava.events.source import EventSource, SourceContext

logger = logging.getLogger("yuyutsava.events.sources.webcam")


class WebcamSource(EventSource):
    """Subprocess-isolated webcam frame producer."""

    name = "webcam"
    topics = ("face.frame",)

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self, ctx: SourceContext) -> None:
        # Lazy import-existence check so machines without cv2 don't crash —
        # they just see one log line and the source idles.
        try:
            import cv2  # type: ignore  # noqa: F401
        except ImportError:
            logger.error(
                "opencv-python not installed; webcam source disabled. "
                "Install with: uv pip install 'yuyutsava[vision]'"
            )
            await ctx.cancelled.wait()
            return

        # Webcam frames go to their own subdir so the BlobSweeper can wipe
        # them aggressively (TTL ~1h) without touching blobs from other
        # sources that may have different retention needs.
        blob_dir_raw = str(
            ctx.params.get("blob_dir") or (Path.home() / ".yuyutsava" / "blobs" / "webcam")
        )
        blob_dir = Path(blob_dir_raw).expanduser()
        blob_dir.mkdir(parents=True, exist_ok=True)

        interval_ms = int(ctx.params.get("interval_ms", 5000))
        motion_threshold = int(ctx.params.get("motion_threshold", 250000))
        camera_index = int(ctx.params.get("camera_index", 0))
        jpeg_quality = int(ctx.params.get("jpeg_quality", 80))
        heartbeat_timeout = float(ctx.params.get("heartbeat_timeout_sec", 8.0))

        cmd = [
            sys.executable, "-m", "yuyutsava.events.sources._webcam_proc",
            "--blob-dir", str(blob_dir),
            "--interval-ms", str(interval_ms),
            "--motion-threshold", str(motion_threshold),
            "--camera-index", str(camera_index),
            "--jpeg-quality", str(jpeg_quality),
        ]
        logger.info(
            "webcam source: spawning subprocess (camera=%d, interval=%dms)",
            camera_index, interval_ms,
        )

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )

        stderr_task = asyncio.create_task(
            self._drain_stderr(self._proc), name="webcam-stderr"
        )
        try:
            await self._read_loop(ctx, self._proc, heartbeat_timeout)
        finally:
            await self._terminate(self._proc)
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._proc = None

    async def _read_loop(
        self,
        ctx: SourceContext,
        proc: asyncio.subprocess.Process,
        heartbeat_timeout: float,
    ) -> None:
        assert proc.stdout is not None
        cancel_task = asyncio.create_task(ctx.cancelled.wait(), name="webcam-cancel-wait")
        try:
            while True:
                read_task = asyncio.create_task(proc.stdout.readline(), name="webcam-readline")
                done, _ = await asyncio.wait(
                    {read_task, cancel_task},
                    timeout=heartbeat_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done:
                    read_task.cancel()
                    return
                if not done:
                    # No line and no cancel → heartbeat missed.
                    read_task.cancel()
                    logger.warning(
                        "webcam: no output for %.1fs; restarting subprocess",
                        heartbeat_timeout,
                    )
                    raise RuntimeError("webcam subprocess silent")

                line = read_task.result()
                if not line:
                    # EOF — child exited.
                    rc = await proc.wait()
                    logger.warning("webcam: subprocess exited with code %s", rc)
                    raise RuntimeError(f"webcam subprocess exited (rc={rc})")

                await self._handle_line(ctx, line)
        finally:
            cancel_task.cancel()

    async def _handle_line(self, ctx: SourceContext, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8", "replace").strip())
        except json.JSONDecodeError:
            logger.debug("webcam: non-JSON stdout line ignored: %r", raw[:120])
            return

        kind = msg.get("kind")
        if kind == "heartbeat" or kind == "ready":
            return
        if kind == "error":
            logger.error("webcam subprocess error: %s", msg.get("msg"))
            return
        if kind != "frame":
            logger.debug("webcam: unknown kind %r", kind)
            return

        blob_path = msg.get("blob_path")
        faces = msg.get("faces") or []
        if not blob_path or not faces:
            return

        payload = {
            "blob_path": blob_path,
            "faces": faces,
            "face_count": len(faces),
            "width": msg.get("width"),
            "height": msg.get("height"),
            "ts": msg.get("ts"),
        }
        await ctx.emit(
            topic="face.frame",
            summary=f"presence detected ({len(faces)} face)",
            payload=payload,
            severity=1,
            hints={"face_count": str(len(faces))},
            blob_path=blob_path,
        )

    @staticmethod
    async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            logger.info("webcam[child]: %s", line.decode("utf-8", "replace").rstrip())

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()  # SIGTERM on POSIX, TerminateProcess on Windows
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("webcam: subprocess didn't exit in 3s; SIGKILL")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        proc = self._proc
        if proc is not None:
            await self._terminate(proc)


register_source("webcam", WebcamSource)
