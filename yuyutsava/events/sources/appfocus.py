"""
App-focus event source (macOS).

Polls ``NSWorkspace.frontmostApplication`` once per second and emits an
``app.focused`` event whenever the frontmost app changes. The event is
informational on its own — it adds context to other events ("the user was
in Slack when this fs.changed fired") rather than triggering work directly.

Config (params from ``events_config.json``)::

    {
      "poll_ms": 1000,           // poll cadence
      "exclude_bundles": [
        "com.electron.yuyutsava" // avoid emitting when our own window is focused
      ]
    }

Other platforms are gated by ``§9 Linux parity`` and produce a single
"unavailable" log line, then idle.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from yuyutsava.events.registry import register_source
from yuyutsava.events.source import EventSource, SourceContext

logger = logging.getLogger("yuyutsava.events.sources.appfocus")


_DEFAULT_EXCLUDE = ("com.electron.yuyutsava",)


class AppFocusSource(EventSource):
    """macOS-only frontmost-app tracker. Idle on other platforms."""

    name = "appfocus"
    topics = ("app.focused",)

    def __init__(self) -> None:
        self._last_bundle: str | None = None

    async def start(self, ctx: SourceContext) -> None:
        if sys.platform != "darwin":
            logger.info(
                "appfocus source: unavailable on %s — idling. See §9 Linux parity.",
                sys.platform,
            )
            await ctx.cancelled.wait()
            return

        try:
            # Imported lazily so non-macOS users don't pay the pyobjc cost.
            from AppKit import NSWorkspace
        except ImportError:
            logger.error("pyobjc-framework-Cocoa not installed; appfocus source disabled")
            await ctx.cancelled.wait()
            return

        poll_ms = int(ctx.params.get("poll_ms", 1000))
        exclude = tuple(ctx.params.get("exclude_bundles") or _DEFAULT_EXCLUDE)
        poll_sec = max(poll_ms, 200) / 1000.0
        workspace = NSWorkspace.sharedWorkspace()

        logger.info("appfocus source: polling every %sms", poll_ms)

        while not ctx.cancelled.is_set():
            try:
                app = workspace.frontmostApplication()
            except Exception:
                logger.warning("appfocus: frontmostApplication() failed", exc_info=True)
                await self._sleep_or_cancel(ctx, max(poll_sec * 4, 2.0))
                continue

            if app is None:
                await self._sleep_or_cancel(ctx, poll_sec)
                continue

            bundle = str(app.bundleIdentifier() or "")
            if bundle in exclude:
                await self._sleep_or_cancel(ctx, poll_sec)
                continue
            if bundle == self._last_bundle:
                await self._sleep_or_cancel(ctx, poll_sec)
                continue
            self._last_bundle = bundle

            name = str(app.localizedName() or "")
            pid = int(app.processIdentifier() or 0)
            payload = {
                "bundle_id": bundle,
                "name": name,
                "pid": pid,
            }
            await ctx.emit(
                topic="app.focused",
                summary=f"focused {name or bundle}",
                payload=payload,
                severity=0,
                hints={"bundle_id": bundle, "name": name},
            )

            await self._sleep_or_cancel(ctx, poll_sec)

    @staticmethod
    async def _sleep_or_cancel(ctx: SourceContext, seconds: float) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(ctx.cancelled.wait()), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def stop(self) -> None:
        return


register_source("appfocus", AppFocusSource)
