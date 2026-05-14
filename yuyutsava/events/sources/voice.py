"""Voice event source — parent half.

Spawns ``python -m yuyutsava.events.sources._voice_proc`` as a subprocess and
bridges its line-delimited JSON output into ``voice.wake`` events on the bus.

Why a subprocess:
  - sounddevice + openwakeword use native audio drivers; blocking I/O and
    TF-backed inference are hostile to the asyncio loop.
  - A driver crash in the child never takes down the daemon.

Heartbeat watchdog: the child writes ``{"kind":"heartbeat"}`` every ~2s. If
no heartbeat (or any line) arrives for ``heartbeat_timeout_sec``
(default 8s = 3 missed beats + slack), the parent SIGTERMs the child and
the registry's exponential backoff respawns this source.

Config (params from ``events_config.json``)::

    {
      "enabled": false,                     // disabled by default — privacy
      "blob_dir": "~/.yuyutsava/blobs/voice",
      "capture_sec": 8,                     // seconds of audio after wake
      "stt_provider": "faster_whisper",     // "faster_whisper" | "groq" | "none"
      "sample_rate": 16000,
      "heartbeat_timeout_sec": 8
    }

Privacy: voice is **disabled by default**. Enable explicitly via
``events_config.json`` or the ``--voice`` daemon flag.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from yuyutsava.events.registry import register_source
from yuyutsava.events.source import EventSource, SourceContext

logger = logging.getLogger("yuyutsava.events.sources.voice")


class VoiceSource(EventSource):
    """Subprocess-isolated wake-word + utterance capture source."""

    name = "voice"
    topics = ("voice.wake",)

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self, ctx: SourceContext) -> None:
        blob_dir_raw = str(
            ctx.params.get("blob_dir") or (Path.home() / ".yuyutsava" / "blobs" / "voice")
        )
        blob_dir = Path(blob_dir_raw).expanduser()
        blob_dir.mkdir(parents=True, exist_ok=True)

        capture_sec = float(ctx.params.get("capture_sec", 8.0))
        stt_provider = str(ctx.params.get("stt_provider", "faster_whisper"))
        sample_rate = int(ctx.params.get("sample_rate", 16000))
        heartbeat_timeout = float(ctx.params.get("heartbeat_timeout_sec", 8.0))

        cmd = [
            sys.executable, "-m", "yuyutsava.events.sources._voice_proc",
            "--blob-dir", str(blob_dir),
            "--capture-sec", str(capture_sec),
            "--stt-provider", stt_provider,
            "--sample-rate", str(sample_rate),
        ]
        logger.info(
            "voice source: spawning subprocess (capture=%.1fs stt=%s)",
            capture_sec, stt_provider,
        )

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )

        stderr_task = asyncio.create_task(
            self._drain_stderr(self._proc), name="voice-stderr"
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
        cancel_task = asyncio.create_task(ctx.cancelled.wait(), name="voice-cancel-wait")
        try:
            while True:
                read_task = asyncio.create_task(proc.stdout.readline(), name="voice-readline")
                done, _ = await asyncio.wait(
                    {read_task, cancel_task},
                    timeout=heartbeat_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done:
                    read_task.cancel()
                    return
                if not done:
                    read_task.cancel()
                    logger.warning(
                        "voice: no output for %.1fs; restarting subprocess",
                        heartbeat_timeout,
                    )
                    raise RuntimeError("voice subprocess silent")

                line = read_task.result()
                if not line:
                    rc = await proc.wait()
                    logger.warning("voice: subprocess exited with code %s", rc)
                    raise RuntimeError(f"voice subprocess exited (rc={rc})")

                await self._handle_line(ctx, line)
        finally:
            cancel_task.cancel()

    async def _handle_line(self, ctx: SourceContext, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8", "replace").strip())
        except json.JSONDecodeError:
            logger.debug("voice: non-JSON stdout line ignored: %r", raw[:120])
            return

        kind = msg.get("kind")
        if kind in ("heartbeat", "ready"):
            return
        if kind == "error":
            logger.error("voice subprocess error: %s", msg.get("msg"))
            return
        if kind != "wake":
            logger.debug("voice: unknown kind %r", kind)
            return

        blob_path = msg.get("blob_path")
        transcript = msg.get("transcript") or ""
        wake_word = msg.get("wake_word") or ""
        duration_sec = msg.get("duration_sec") or 0.0

        summary = f"voice wake: {transcript!r}" if transcript else f"voice wake ({wake_word})"
        payload = {
            "blob_path": blob_path,
            "transcript": transcript,
            "wake_word": wake_word,
            "duration_sec": duration_sec,
            "ts": msg.get("ts"),
        }
        await ctx.emit(
            topic="voice.wake",
            summary=summary,
            payload=payload,
            severity=2,
            hints={"wake_word": wake_word, "has_transcript": "1" if transcript else "0"},
            blob_path=blob_path,
        )

    @staticmethod
    async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            logger.info("voice[child]: %s", line.decode("utf-8", "replace").rstrip())

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("voice: subprocess didn't exit in 3s; SIGKILL")
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


register_source("voice", VoiceSource)
