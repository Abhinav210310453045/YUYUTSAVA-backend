"""
Global hotkey event source.

Each binding maps a key combination to a *semantic action* (a short string the
triage agent or a downstream rule consumes). When the user presses a bound
combo a ``hotkey.pressed`` event fires with the action name and the focused-
app context, if available.

Config (params from ``events_config.json``)::

    {
      "bindings": {
        "<cmd>+<shift>+y": "ask",
        "<cmd>+<shift>+u": "summarize_clipboard"
      }
    }

Key syntax follows :func:`pynput.keyboard.GlobalHotKeys` — e.g. ``<cmd>+<shift>+y``
on macOS, ``<ctrl>+<shift>+y`` on Windows / Linux. macOS requires Accessibility
entitlement on the parent app for global hotkeys to work; otherwise pynput's
listener will succeed on init but never fire.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from yuyutsava.events.registry import register_source
from yuyutsava.events.source import EventSource, SourceContext

logger = logging.getLogger("yuyutsava.events.sources.hotkey")


class HotkeySource(EventSource):
    """Global hotkey listener backed by ``pynput.keyboard.GlobalHotKeys``."""

    name = "hotkey"
    topics = ("hotkey.pressed",)

    def __init__(self) -> None:
        self._listener: Any | None = None

    async def start(self, ctx: SourceContext) -> None:
        try:
            from pynput import keyboard
        except ImportError:
            logger.error("pynput is not installed; hotkey source disabled")
            await ctx.cancelled.wait()
            return

        bindings_raw = ctx.params.get("bindings") or {}
        if not isinstance(bindings_raw, dict) or not bindings_raw:
            logger.info("hotkey source: no bindings configured; idle")
            await ctx.cancelled.wait()
            return

        loop = asyncio.get_running_loop()

        # Build pynput's callback map: keycombo string → no-arg callable.
        # Each callable trampolines onto the asyncio loop so we don't touch
        # the bus from the listener thread.
        def _make_cb(combo: str, action: str):
            def _fire() -> None:
                try:
                    loop.call_soon_threadsafe(
                        asyncio.create_task,
                        self._emit_event(ctx, combo=combo, action=action),
                    )
                except RuntimeError:
                    # Loop closed during shutdown.
                    pass
            return _fire

        callbacks: dict[str, Any] = {}
        for combo, action in bindings_raw.items():
            if not isinstance(combo, str) or not isinstance(action, str):
                logger.warning("hotkey: skipping malformed binding %r → %r", combo, action)
                continue
            callbacks[combo] = _make_cb(combo, str(action))

        if not callbacks:
            logger.info("hotkey source: no valid bindings; idle")
            await ctx.cancelled.wait()
            return

        try:
            listener = keyboard.GlobalHotKeys(callbacks)
            listener.start()
            self._listener = listener
            logger.info("hotkey source: %d binding(s) active: %s",
                        len(callbacks), ", ".join(callbacks.keys()))
        except Exception:
            logger.exception("hotkey: failed to start global listener")
            await ctx.cancelled.wait()
            return

        try:
            await ctx.cancelled.wait()
        finally:
            try:
                listener.stop()
            except Exception:
                pass
            self._listener = None

    async def _emit_event(self, ctx: SourceContext, *, combo: str, action: str) -> None:
        payload = {"combo": combo, "action": action}
        await ctx.emit(
            topic="hotkey.pressed",
            summary=f"hotkey {combo} → {action}",
            payload=payload,
            severity=2,
            hints={"action": action, "combo": combo},
        )

    async def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None


register_source("hotkey", HotkeySource)
