"""``first_chunk_retry`` — a 429 before the first streamed chunk is retried, nothing else is.

The Vertex SDK retries only the stream *open*; with grpc.aio the 429 arrives on
the first read and escapes it. These tests drive the mixin with a fake base
whose ``_astream``/``_stream`` fail a scripted number of times, with the sleeps
patched out. No model is built, no network, no API calls.

Run:  .venv/bin/python test/llm/test_first_chunk_retry.py
"""

from __future__ import annotations

import asyncio
import unittest

from google.api_core import exceptions as gexc

from yuyutsava.llm.quirks import first_chunk_retry as fcr
from yuyutsava.llm.quirks.first_chunk_retry import first_chunk_retry


class _Base:
    """Stand-in for a chat model: yields ``chunks`` after ``fail_first`` attempts
    that raise ``exc`` before any chunk; ``fail_after_first`` raises mid-stream."""

    max_retries = 6
    wait_exponential_kwargs = None
    model_name = "fake-gemini"

    def __init__(self, *, fail_first=0, exc=None, chunks=("a", "b", "c"), fail_after_first=False):
        self.fail_first = fail_first
        self.exc = exc or gexc.ResourceExhausted("429 quota")
        self.chunks = list(chunks)
        self.fail_after_first = fail_after_first
        self.attempts = 0

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.attempts += 1
        if self.attempts <= self.fail_first:
            raise self.exc
        for i, c in enumerate(self.chunks):
            if i == 1 and self.fail_after_first:
                raise self.exc
            yield c

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        self.attempts += 1
        if self.attempts <= self.fail_first:
            raise self.exc
        for i, c in enumerate(self.chunks):
            if i == 1 and self.fail_after_first:
                raise self.exc
            yield c


Retrying = first_chunk_retry(_Base)


def _collect(model):
    async def run():
        return [c async for c in model._astream([])]

    return asyncio.run(run())


class _NoSleep:
    def __init__(self):
        self.delays: list[float] = []
        self._saved = None

    async def _asleep(self, d):
        self.delays.append(d)

    def _sleep(self, d):
        self.delays.append(d)

    def __enter__(self):
        self._saved = (fcr._asleep, fcr._sleep)
        fcr._asleep, fcr._sleep = self._asleep, self._sleep
        return self

    def __exit__(self, *_a):
        fcr._asleep, fcr._sleep = self._saved


class FirstChunkRetryTests(unittest.TestCase):
    def test_class_identity_is_cached(self):
        self.assertIs(first_chunk_retry(_Base), Retrying)
        self.assertEqual(Retrying.__name__, "FirstChunkRetry_Base")

    def test_transient_before_first_chunk_is_retried(self):
        m = Retrying(fail_first=2)
        with _NoSleep() as ns, self.assertLogs("yuyutsava.llm.retry", level="WARNING") as logs:
            out = _collect(m)
        self.assertEqual(out, ["a", "b", "c"])
        self.assertEqual(m.attempts, 3)
        self.assertEqual(len(logs.records), 2)
        self.assertEqual(len(ns.delays), 2)
        self.assertTrue(all(1.0 <= d <= 30.0 for d in ns.delays))
        self.assertIn("ResourceExhausted", logs.records[0].getMessage())

    def test_sync_stream_mirrors_async(self):
        m = Retrying(fail_first=1)
        with _NoSleep() as ns:
            out = list(m._stream([]))
        self.assertEqual(out, ["a", "b", "c"])
        self.assertEqual(m.attempts, 2)
        self.assertEqual(len(ns.delays), 1)

    def test_service_unavailable_is_transient_too(self):
        m = Retrying(fail_first=1, exc=gexc.ServiceUnavailable("503"))
        with _NoSleep():
            self.assertEqual(_collect(m), ["a", "b", "c"])

    def test_non_transient_error_propagates_at_once(self):
        m = Retrying(fail_first=1, exc=gexc.InvalidArgument("400 bad request"))
        with _NoSleep() as ns, self.assertRaises(gexc.InvalidArgument):
            _collect(m)
        self.assertEqual(m.attempts, 1)
        self.assertEqual(ns.delays, [])

    def test_error_after_first_chunk_is_not_retried(self):
        m = Retrying(fail_after_first=True)
        with _NoSleep() as ns, self.assertRaises(gexc.ResourceExhausted):
            _collect(m)
        self.assertEqual(m.attempts, 1)
        self.assertEqual(ns.delays, [])

    def test_retries_exhausted_reraises_original(self):
        m = Retrying(fail_first=10)
        m.max_retries = 2
        with _NoSleep() as ns, self.assertRaises(gexc.ResourceExhausted):
            _collect(m)
        self.assertEqual(m.attempts, 3)
        self.assertEqual(len(ns.delays), 2)

    def test_zero_retries_means_no_retry(self):
        m = Retrying(fail_first=1)
        m.max_retries = 0
        with _NoSleep() as ns, self.assertRaises(gexc.ResourceExhausted):
            _collect(m)
        self.assertEqual(m.attempts, 1)
        self.assertEqual(ns.delays, [])

    def test_backoff_honours_model_wait_kwargs(self):
        m = Retrying()
        m.wait_exponential_kwargs = {"multiplier": 1.0, "min": 4.0, "max": 10.0}
        # attempt 0: 1*2**0=1 → floored to 4; attempt 5: 32 → capped at 10. Jitter ∈ [0.5, 1].
        self.assertTrue(2.0 <= fcr._backoff(0, m) <= 4.0)
        self.assertTrue(5.0 <= fcr._backoff(5, m) <= 10.0)

    def test_empty_stream_is_passed_through(self):
        m = Retrying(chunks=())
        self.assertEqual(_collect(m), [])
        self.assertEqual(m.attempts, 1)


if __name__ == "__main__":
    unittest.main()
