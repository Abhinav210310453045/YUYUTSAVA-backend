"""
Clipboard event source.

Polls the system clipboard via :mod:`pyperclip` every ``poll_ms`` (default 500 ms)
and emits a ``clipboard.copied`` event whenever the SHA-256 of the textual
contents changes. Dedup is critical — without it a single Cmd+C produces an
event per poll until the user copies something else.

Config (params from ``events_config.json``)::

    {
      "poll_ms": 500,
      "ignore_empty": true,
      "max_chars": 16384            // payloads longer than this are truncated
    }

The source classifies content by simple heuristics (``kind`` hint):

  - ``url``   → ``http(s)://`` or ``www.`` prefix.
  - ``path``  → resolves to an existing path on disk.
  - ``text``  → everything else.

Image clipboard contents are not supported by pyperclip; an image copy looks
like an empty clipboard read and is dropped by ``ignore_empty``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path

from yuyutsava.events.registry import register_source
from yuyutsava.events.source import EventSource, SourceContext

logger = logging.getLogger("yuyutsava.events.sources.clipboard")


_URL_RE = re.compile(r"^\s*(https?://|www\.)\S", re.IGNORECASE)


def _classify(text: str) -> str:
    """Cheap one-pass content classifier — returns the ``kind`` hint."""
    stripped = text.strip()
    if _URL_RE.match(stripped):
        return "url"
    if len(stripped) < 1024:
        try:
            p = Path(stripped).expanduser()
            if p.exists():
                return "path"
        except (OSError, ValueError):
            pass
    return "text"


class ClipboardSource(EventSource):
    """Polling clipboard watcher. macOS / Linux (X11) / Windows via pyperclip."""

    name = "clipboard"
    topics = ("clipboard.copied",)

    def __init__(self) -> None:
        self._last_hash: str | None = None

    async def start(self, ctx: SourceContext) -> None:
        try:
            import pyperclip  # local — heavy on first import, cheap thereafter
        except ImportError:
            logger.error("pyperclip is not installed; clipboard source disabled")
            await ctx.cancelled.wait()
            return

        poll_ms = int(ctx.params.get("poll_ms", 500))
        ignore_empty = bool(ctx.params.get("ignore_empty", True))
        max_chars = int(ctx.params.get("max_chars", 16384))
        poll_sec = max(poll_ms, 50) / 1000.0

        loop = asyncio.get_running_loop()

        # Prime the hash so we don't emit on whatever was already on the
        # clipboard when the daemon started.
        try:
            initial = await loop.run_in_executor(None, pyperclip.paste)
            if initial:
                self._last_hash = hashlib.sha256(initial.encode("utf-8", "replace")).hexdigest()
        except Exception:
            logger.debug("clipboard: initial read failed; treating as empty", exc_info=True)

        logger.info("clipboard source: polling every %sms", poll_ms)

        while not ctx.cancelled.is_set():
            try:
                text = await loop.run_in_executor(None, pyperclip.paste)
            except Exception:
                # pyperclip raises on backend errors (no X server, etc.).
                # Log once and back off; future polls will retry.
                logger.warning("clipboard: paste failed; retrying", exc_info=True)
                await self._sleep_or_cancel(ctx, max(poll_sec * 4, 2.0))
                continue

            if text is None or (ignore_empty and not text.strip()):
                await self._sleep_or_cancel(ctx, poll_sec)
                continue

            digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
            if digest == self._last_hash:
                await self._sleep_or_cancel(ctx, poll_sec)
                continue
            self._last_hash = digest

            truncated = text if len(text) <= max_chars else text[:max_chars]
            kind = _classify(truncated)
            preview = truncated.replace("\n", " ")[:80]
            payload = {
                "kind": kind,
                "content": truncated,
                "length": len(text),
                "truncated": len(text) > max_chars,
                "sha256": digest,
            }
            await ctx.emit(
                topic="clipboard.copied",
                summary=f"copied {kind} ({len(text)} chars): {preview}",
                payload=payload,
                severity=1,
                hints={"kind": kind, "length": str(len(text))},
            )

            await self._sleep_or_cancel(ctx, poll_sec)

    @staticmethod
    async def _sleep_or_cancel(ctx: SourceContext, seconds: float) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(ctx.cancelled.wait()), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def stop(self) -> None:
        # Nothing to release — the polling loop exits when ctx.cancelled is set.
        return


register_source("clipboard", ClipboardSource)
