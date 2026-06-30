"""The Announcer — the daemon's single, serialized "make a sound" service.

Any subsystem that wants to speak text or play an earcon on the **daemon host**
goes through one :class:`Announcer` instance instead of touching TTS / PortAudio
directly. That keeps three things true:

* **Decoupling** — the Announcer knows nothing about agents, conversations, or
  proposals. The voice agent, the notification system, and the proposal channel
  are all just callers. (See the package docstring.)
* **No overlap** — a single background worker drains an internal queue, so two
  callers speaking at once are serialized rather than talking over each other.
* **Graceful degradation** — when audio or TTS is unavailable (headless host,
  ``PIPER_MODEL`` unset), calls log and return instead of raising.

``say()`` / ``play_earcon()`` await until their sound has finished playing, so a
caller that needs to listen afterwards (e.g. the voice channel) can simply
``await announcer.say(...)`` then start capturing. ``stop()`` cuts off the
current sound and drops the queue for barge-in.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from yuyutsava.audio_io.earcons import earcon_path
from yuyutsava.io.audio import AudioUnavailableError, play_wav, stop_playback
from yuyutsava.io.tts import TTS, tts_from_env

logger = logging.getLogger("yuyutsava.audio_io.announcer")


@dataclass
class _Item:
    kind: str  # "say" | "earcon"
    payload: str  # text to speak, or earcon name
    done: asyncio.Future


class Announcer:
    """Serialized text-to-speech + earcon playback for the daemon host."""

    def __init__(
        self,
        *,
        tts_factory: Callable[[], TTS] | None = None,
        tmp_dir: Path | None = None,
    ) -> None:
        self._tts_factory = tts_factory or tts_from_env
        self._tts: TTS | None = None
        self._tts_failed = False  # remember an unrecoverable TTS build failure
        self._tmp = tmp_dir or Path(tempfile.mkdtemp(prefix="yuyutsava_announcer_"))
        self._queue: asyncio.Queue[_Item] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._seq = 0

    # -- public API --------------------------------------------------------

    async def say(self, text: str) -> None:
        """Speak ``text``; resolves once playback finishes (or is skipped)."""
        text = (text or "").strip()
        if not text:
            return
        await self._submit("say", text)

    async def play_earcon(self, name: str) -> None:
        """Play the named earcon; resolves once playback finishes."""
        await self._submit("earcon", name)

    def stop(self) -> None:
        """Barge-in: cut off the current sound and drop everything queued."""
        stop_playback()
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - race guard
                break
            if not item.done.done():
                item.done.set_result(None)
            self._queue.task_done()

    async def aclose(self) -> None:
        """Stop the worker and clean up the temp dir."""
        self.stop()
        worker = self._worker
        self._worker = None
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            import shutil
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    # -- internals ---------------------------------------------------------

    async def _submit(self, kind: str, payload: str) -> None:
        loop = asyncio.get_running_loop()
        item = _Item(kind=kind, payload=payload, done=loop.create_future())
        self._ensure_worker()
        await self._queue.put(item)
        await item.done

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.ensure_future(self._run())

    def _get_tts(self) -> TTS | None:
        """Lazily build the TTS backend; cache the result (and any failure)."""
        if self._tts is not None or self._tts_failed:
            return self._tts
        try:
            self._tts = self._tts_factory()
        except Exception:  # noqa: BLE001
            self._tts_failed = True
            logger.warning("Announcer: TTS unavailable — say() will be silent", exc_info=True)
        return self._tts

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item.kind == "earcon":
                    await self._play_earcon(item.payload)
                else:
                    await self._speak(item.payload)
            except asyncio.CancelledError:
                if not item.done.done():
                    item.done.set_result(None)
                raise
            except Exception:  # noqa: BLE001
                logger.warning("Announcer: playback failed", exc_info=True)
            finally:
                if not item.done.done():
                    item.done.set_result(None)
                self._queue.task_done()

    async def _speak(self, text: str) -> None:
        tts = self._get_tts()
        if tts is None:
            return
        self._seq += 1
        out = self._tmp / f"say_{self._seq}.wav"
        try:
            await tts.synthesize(text, out)
            await play_wav(out)
        except AudioUnavailableError:
            logger.debug("Announcer: audio unavailable — silent")

    async def _play_earcon(self, name: str) -> None:
        try:
            await play_wav(earcon_path(name))
        except AudioUnavailableError:
            logger.debug("Announcer: audio unavailable — earcon silent")
        except KeyError:
            logger.warning("Announcer: unknown earcon %r", name)


def announcer_from_env() -> Announcer:
    """Build an Announcer using the env-configured TTS backend (lazy).

    TTS is only constructed on the first ``say()``, so this never raises for a
    missing ``PIPER_MODEL`` — earcons work regardless.
    """
    return Announcer()
