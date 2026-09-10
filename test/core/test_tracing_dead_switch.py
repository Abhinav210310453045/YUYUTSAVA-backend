"""Langfuse must be silent when it is not there — at start-up or mid-session.

Two behaviours pinned here:

1. ``silence_plumbing_loggers`` really silences the OTEL exporter's *child*
   logger. The original code set ``propagate=False``/``disabled=True`` on the
   parent but attached no handler, so a child's ERROR record found no handler
   at all and Python printed it through ``logging.lastResort`` — that is how
   "Failed to export span batch code: 404" reached the terminal.
2. The dead-switch exporter: the first batch that fails to land turns tracing
   off for the process (``get_callback`` → None) and every later batch is
   dropped without a request. A local HTTP server answering 404 stands in for
   "something else took Langfuse's port" — no network beyond localhost.

Run:  .venv/bin/python test/core/test_tracing_dead_switch.py
"""

from __future__ import annotations

import io
import logging
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from yuyutsava.core import tracing


class _Count404(BaseHTTPRequestHandler):
    hits = 0

    def do_POST(self):  # noqa: N802
        type(self).hits += 1
        self.send_response(404)
        self.end_headers()

    do_GET = do_POST  # noqa: N815 — the health probe must also see a 404

    def log_message(self, *_a):  # keep the test output clean
        pass


class _Server:
    def __enter__(self):
        _Count404.hits = 0
        self.httpd = HTTPServer(("127.0.0.1", 0), _Count404)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __exit__(self, *_a):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Env:
    def __init__(self, host: str):
        self.values = {
            "LANGFUSE_ENABLED": "1",
            "LANGFUSE_HOST": host,
            "LANGFUSE_PUBLIC_KEY": "pk-test",
            "LANGFUSE_SECRET_KEY": "sk-test",
            "LANGFUSE_TIMEOUT": "2",
        }
        self.saved: dict[str, str | None] = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            os.environ[k] = v
        tracing.reset_reachability_cache()
        return self

    def __exit__(self, *_a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        tracing.reset_reachability_cache()


class SilencerTests(unittest.TestCase):
    def test_child_of_silenced_logger_does_not_fall_through_to_lastresort(self):
        from yuyutsava.core.engine import silence_plumbing_loggers

        silence_plumbing_loggers()
        child = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        captured = io.StringIO()
        saved = logging.lastResort
        logging.lastResort = logging.StreamHandler(captured)
        try:
            child.error("Failed to export span batch code: %s, reason: %s", 404, "Not Found")
        finally:
            logging.lastResort = saved
        self.assertEqual(captured.getvalue(), "")

    def test_silencer_is_idempotent_on_handlers(self):
        from yuyutsava.core.engine import silence_plumbing_loggers

        silence_plumbing_loggers()
        silence_plumbing_loggers()
        lg = logging.getLogger("opentelemetry")
        nulls = [h for h in lg.handlers if isinstance(h, logging.NullHandler)]
        self.assertEqual(len(nulls), 1)


class DeadSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Mirror the real entrypoints (CLI + daemon), which install the silencer
        # at start-up; without it the exporter's own ERROR line is visible here.
        from yuyutsava.core.engine import silence_plumbing_loggers

        silence_plumbing_loggers()

    def test_startup_probe_404_means_off_and_no_client(self):
        with _Server() as host, _Env(host):
            self.assertIsNone(tracing.get_callback())
            self.assertFalse(tracing._langfuse_reachable())
            self.assertIsNone(tracing._client)

    def test_first_failed_export_flips_the_switch_and_stops_requests(self):
        with _Server() as host, _Env(host):
            exporter = tracing._build_exporter()
            from opentelemetry.sdk.trace.export import SpanExportResult

            with self.assertLogs("yuyutsava.core.tracing", level="INFO") as logs:
                self.assertIs(exporter.export([]), SpanExportResult.SUCCESS)
            self.assertTrue(tracing.is_dead())
            self.assertFalse(tracing._reachable)
            self.assertEqual(len(logs.records), 1)
            hits_after_first = _Count404.hits
            self.assertGreaterEqual(hits_after_first, 1)

            # Second batch: dropped locally, no request, no log line.
            with self.assertNoLogs("yuyutsava.core.tracing", level="INFO"):
                self.assertIs(exporter.export([]), SpanExportResult.SUCCESS)
            self.assertEqual(_Count404.hits, hits_after_first)
            self.assertIsNone(tracing.get_callback())

    def test_mid_session_drop_through_the_real_client(self):
        """Langfuse was up at start-up (simulated by pre-seeding the cache), then
        its port answers 404: the client we built carries our exporter, so one
        flush kills tracing and get_callback stops handing out handlers."""
        from langfuse._client.resource_manager import LangfuseResourceManager

        with _Server() as host, _Env(host):
            tracing._reachable = True  # "it was up when we probed"
            try:
                cb = tracing.get_callback()
                self.assertIsNotNone(cb)
                client = tracing._client
                self.assertIsNotNone(client)
                client.start_observation(name="probe", as_type="span").end()
                with self.assertLogs("yuyutsava.core.tracing", level="INFO"):
                    client.flush()
                self.assertTrue(tracing.is_dead())
                self.assertIsNone(tracing.get_callback())
            finally:
                LangfuseResourceManager.reset()


if __name__ == "__main__":
    unittest.main()
