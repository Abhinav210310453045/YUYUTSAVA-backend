"""The daemon's own log records reach the UI.

The Logs panel contained exactly one kind of line — `GET /tasks → 200 (3ms)`,
the UI's own polling traffic — because `http_log` was the only kind routed to
it. Meanwhile ~500 `logger.*` calls across ~150 loggers (model calls, retries,
compactions, offloads, storage failover, caught exceptions) went to stderr,
where no user can see them, and the titlebar's log-level dropdown changed
*that* stream so it appeared to do nothing.

These pin the properties a log transport has to have:

* records arrive, structured — level and logger as fields, not flattened
  into prose, or the panel cannot colour or filter them;
* the logger's own level gates it, so `PUT /logs/level` controls the panel;
* it never blocks the caller: `emit` only appends;
* it cannot feed itself — broadcasting can log, and that log must not be
  broadcast;
* a flood is dropped, bounded and *reported*, not silently lost or grown.

Run:  .venv/bin/python test/daemon/test_log_bridge.py
"""

from __future__ import annotations

import asyncio
import logging
import unittest

import yuyutsava.core.config  # noqa: F401 — import first (core/__init__ cycle)
from yuyutsava.daemon.channels import AppLogPayload
from yuyutsava.daemon.log_bridge import MAX_QUEUE, LogBridge


class _Hub:
    """Captures what would have gone out over SSE."""

    def __init__(self, fail: bool = False) -> None:
        self.items: list = []
        self._fail = fail

    async def broadcast(self, item) -> None:
        if self._fail:
            raise RuntimeError("no subscribers")
        self.items.append(item)


class _Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.log = logging.getLogger("yuyutsava.test.bridge")
        self.root = logging.getLogger("yuyutsava")
        self._saved_level = self.root.level
        self.root.setLevel(logging.INFO)
        self.hub = _Hub()
        self.bridge = LogBridge(self.hub)

    async def asyncTearDown(self) -> None:
        await self.bridge.aclose()
        self.root.setLevel(self._saved_level)

    async def _settle(self) -> None:
        for _ in range(40):
            await asyncio.sleep(0.02)
            if self.hub.items:
                return


class Delivery(_Case):
    async def test_a_record_arrives_as_a_structured_payload(self):
        self.bridge.install()
        self.log.info("compacted 18 msgs into a summary")
        await self._settle()
        self.assertTrue(self.hub.items, "nothing reached the hub")
        payload = self.hub.items[0].payload
        self.assertIsInstance(payload, AppLogPayload)
        self.assertEqual(payload.kind, "app_log")
        self.assertEqual(payload.level, "INFO")
        self.assertEqual(payload.logger, "yuyutsava.test.bridge")
        self.assertIn("compacted 18", payload.message)

    async def test_level_and_logger_are_fields_not_prose(self):
        # The panel colours by level and filters by logger; both are
        # impossible once they have been formatted into the message.
        self.bridge.install()
        self.log.warning("provider busy")
        await self._settle()
        payload = self.hub.items[0].payload
        self.assertEqual(payload.level, "WARNING")
        self.assertNotIn("WARNING", payload.message)

    async def test_printf_style_arguments_are_rendered(self):
        self.bridge.install()
        self.log.info("offloaded %d chars to %s", 1234, "art_9")
        await self._settle()
        self.assertIn("offloaded 1234 chars to art_9",
                      self.hub.items[0].payload.message)

    async def test_an_exception_is_summarised_on_one_line(self):
        # The panel is a list, not a traceback viewer; the full trace stays on
        # stderr for whoever needs it.
        self.bridge.install()
        try:
            raise ValueError("bad dsn")
        except ValueError:
            self.log.exception("storage probe failed")
        await self._settle()
        msg = self.hub.items[0].payload.message
        self.assertIn("storage probe failed", msg)
        self.assertIn("ValueError: bad dsn", msg)
        self.assertNotIn("\n", msg)


class LevelGating(_Case):
    async def test_the_logger_level_decides_what_is_forwarded(self):
        # This is what makes PUT /logs/level (the titlebar dropdown) control
        # the panel, instead of only changing the daemon's stderr.
        self.root.setLevel(logging.WARNING)
        self.bridge.install()
        self.log.info("not important enough")
        self.log.warning("this one counts")
        await self._settle()
        messages = [i.payload.message for i in self.hub.items]
        self.assertIn("this one counts", messages)
        self.assertNotIn("not important enough", messages)

    async def test_lowering_the_level_lets_debug_through(self):
        self.root.setLevel(logging.DEBUG)
        self.bridge.install()
        self.log.debug("cache prefix reused")
        await self._settle()
        self.assertIn("cache prefix reused",
                      [i.payload.message for i in self.hub.items])


class Safety(_Case):
    async def test_emit_does_not_touch_the_hub(self):
        # emit() runs on whatever thread logged, possibly with no event loop.
        # It must only append.
        self.bridge.install()
        self.log.info("queued only")
        self.assertEqual(self.hub.items, [])

    async def test_the_bridge_does_not_forward_its_own_logs(self):
        # Broadcasting can log; that log must not be broadcast, or one failure
        # becomes an infinite loop.
        self.bridge.install()
        logging.getLogger("yuyutsava.daemon.log_bridge").error("boom")
        logging.getLogger("yuyutsava.daemon.channels").error("boom")
        await asyncio.sleep(0.1)
        self.assertEqual(self.hub.items, [])

    async def test_a_flood_is_bounded_and_reported(self):
        self.bridge.install()
        for i in range(MAX_QUEUE + 500):
            self.log.info("flood %d", i)
        # Never more than the cap is retained.
        self.assertLessEqual(len(self.bridge._queue), MAX_QUEUE)
        await self._settle()
        for _ in range(60):
            await asyncio.sleep(0.02)
            if any("dropped" in i.payload.message for i in self.hub.items):
                break
        self.assertTrue(
            any("dropped" in i.payload.message for i in self.hub.items),
            "a dropped batch must be reported, not silently lost",
        )

    async def test_a_failing_hub_does_not_kill_the_bridge(self):
        bridge = LogBridge(_Hub(fail=True))
        bridge.install()
        try:
            self.log.info("into the void")
            await asyncio.sleep(0.1)
            self.assertFalse(bridge._task.done(), "the drain task died")
        finally:
            await bridge.aclose()

    async def test_install_is_idempotent(self):
        self.bridge.install()
        self.bridge.install()
        handlers = [h for h in self.root.handlers if h is self.bridge._handler]
        self.assertEqual(len(handlers), 1)

    async def test_close_detaches_the_handler(self):
        self.bridge.install()
        await self.bridge.aclose()
        self.assertNotIn(self.bridge._handler, self.root.handlers)
        self.assertIsNone(self.bridge._task)


class PanelRouting(unittest.TestCase):
    def test_the_ui_routes_app_log_to_the_logs_tab(self):
        from pathlib import Path

        src = Path("electron-app/src/renderer/hooks/useSSE.jsx").read_text()
        self.assertIn("'app_log'", src)
        self.assertIn("LOG_KINDS", src)
        # The structured fields must survive into the line the panel renders.
        self.assertIn("level:", src)
        self.assertIn("logger:", src)

    def test_the_payload_is_in_the_channel_union(self):
        import inspect

        from yuyutsava.daemon import channels

        src = inspect.getsource(channels)
        self.assertIn("| AppLogPayload", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
