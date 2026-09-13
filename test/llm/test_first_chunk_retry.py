"""``first_chunk_retry`` — a 429 before the first streamed chunk is retried, nothing else is.

The Vertex SDK retries only the stream *open*; with grpc.aio the 429 arrives on
the first read and escapes it. These tests drive the mixin with a fake base
whose ``_astream``/``_stream`` fail a scripted number of times, with the sleeps
patched out. No model is built, no network, no API calls.

Run:  .venv/bin/python test/llm/test_first_chunk_retry.py
"""

from __future__ import annotations

import asyncio
import os
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


class _FakeClock:
    """Monotonic clock that only advances by the delays the ladder asks for."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class RetryBudgetTests(unittest.TestCase):
    """A wall-clock ceiling across all attempts.

    ``max_retries`` bounds the attempt count, not the time: six attempts on
    the default ladder is minutes of a frozen spinner, and during the 12 Sep
    session the provider's own decorator was nested inside each one. The turn
    has to come back and say what happened.
    """

    def setUp(self):
        self.clock = _FakeClock()
        self._saved = fcr._now
        fcr._now = self.clock
        self._env = os.environ.get("VERTEX_RETRY_BUDGET_SEC")
        self.addCleanup(self._restore)

    def _restore(self):
        fcr._now = self._saved
        if self._env is None:
            os.environ.pop("VERTEX_RETRY_BUDGET_SEC", None)
        else:
            os.environ["VERTEX_RETRY_BUDGET_SEC"] = self._env

    def _advancing(self):
        """A no-sleep patch that also advances the fake clock."""
        ns = _NoSleep()
        real_asleep, real_sleep = ns._asleep, ns._sleep

        async def asleep(d):
            self.clock.t += d
            await real_asleep(d)

        def sleep(d):
            self.clock.t += d
            real_sleep(d)

        ns._asleep, ns._sleep = asleep, sleep
        return ns

    def test_budget_stops_the_ladder_early(self):
        os.environ["VERTEX_RETRY_BUDGET_SEC"] = "10"
        m = Retrying(fail_first=10)  # never succeeds
        with self._advancing() as ns, self.assertRaises(gexc.ResourceExhausted):
            _collect(m)
        # Ladder is 2,4,8,… so the third wait would pass 10s: it stops before
        # sleeping, rather than after.
        self.assertLessEqual(self.clock.t, 10.0)
        self.assertLess(m.attempts, 7)
        self.assertGreaterEqual(m.attempts, 2)  # it did retry at least once
        self.assertLess(sum(ns.delays), 10.0)

    def test_budget_gives_up_with_an_explanatory_log(self):
        os.environ["VERTEX_RETRY_BUDGET_SEC"] = "1"
        m = Retrying(fail_first=10)
        with self._advancing(), self.assertLogs("yuyutsava.llm.retry", level="WARNING") as logs:
            with self.assertRaises(gexc.ResourceExhausted):
                _collect(m)
        blob = " ".join(r.getMessage() for r in logs.records)
        self.assertIn("VERTEX_RETRY_BUDGET_SEC", blob)
        self.assertIn("still busy", blob)

    def test_a_generous_budget_does_not_interfere(self):
        os.environ["VERTEX_RETRY_BUDGET_SEC"] = "600"
        m = Retrying(fail_first=2)
        with self._advancing():
            self.assertEqual(_collect(m), ["a", "b", "c"])
        self.assertEqual(m.attempts, 3)

    def test_bad_or_zero_budget_falls_back_to_the_default(self):
        for raw in ("", "nonsense", "0", "-5"):
            with self.subTest(raw=raw):
                os.environ["VERTEX_RETRY_BUDGET_SEC"] = raw
                self.assertEqual(fcr._budget_sec(), fcr._DEFAULT_BUDGET_SEC)


class RetryListenerTests(unittest.TestCase):
    """The front needs to say "provider busy, retrying" in its own voice."""

    def tearDown(self):
        fcr.set_retry_listener(None)

    def test_listener_is_called_per_retry(self):
        seen = []
        fcr.set_retry_listener(lambda *a: seen.append(a))
        m = Retrying(fail_first=2)
        with _NoSleep():
            _collect(m)

        self.assertEqual(len(seen), 2)
        model, attempt, retries, delay, exc = seen[0]
        self.assertEqual(model, "fake-gemini")
        self.assertEqual((attempt, retries), (1, 6))
        self.assertGreater(delay, 0)
        self.assertIsInstance(exc, gexc.ResourceExhausted)
        self.assertEqual(seen[1][1], 2)

    def test_listener_is_not_called_when_nothing_is_retried(self):
        seen = []
        fcr.set_retry_listener(lambda *a: seen.append(a))
        with _NoSleep():
            _collect(Retrying())
        self.assertEqual(seen, [])

    def test_a_broken_listener_never_breaks_the_call(self):
        def boom(*_a):
            raise RuntimeError("listener bug")

        fcr.set_retry_listener(boom)
        m = Retrying(fail_first=1)
        with _NoSleep():
            self.assertEqual(_collect(m), ["a", "b", "c"])

    def test_clearing_the_listener_works(self):
        seen = []
        fcr.set_retry_listener(lambda *a: seen.append(a))
        fcr.set_retry_listener(None)
        with _NoSleep():
            _collect(Retrying(fail_first=1))
        self.assertEqual(seen, [])


class QuirkOwnsTheLadderTests(unittest.TestCase):
    """VERTEX_MAX_RETRIES steers the quirk, not the SDK's nested decorator."""

    def setUp(self):
        self._env = os.environ.get("VERTEX_MAX_RETRIES")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._env is None:
            os.environ.pop("VERTEX_MAX_RETRIES", None)
        else:
            os.environ["VERTEX_MAX_RETRIES"] = self._env

    def test_env_overrides_the_models_lowered_field(self):
        # The provider pins the model's own max_retries to <=2; the quirk must
        # not inherit that as its own ladder length.
        m = Retrying(fail_first=10)
        m.max_retries = 2
        os.environ["VERTEX_MAX_RETRIES"] = "5"
        with _NoSleep() as ns, self.assertRaises(gexc.ResourceExhausted):
            _collect(m)
        self.assertEqual(len(ns.delays), 5)

    def test_model_field_is_the_fallback(self):
        os.environ.pop("VERTEX_MAX_RETRIES", None)
        m = Retrying(fail_first=10)
        m.max_retries = 1
        with _NoSleep() as ns, self.assertRaises(gexc.ResourceExhausted):
            _collect(m)
        self.assertEqual(len(ns.delays), 1)

    def test_provider_pins_the_sdk_ladder_low(self):
        # Guards the pairing: if this line moves back to settings.max_retries
        # the two ladders multiply again (6x6 = minutes per call).
        import inspect

        from yuyutsava.llm.providers import vertex

        src = inspect.getsource(vertex.VertexProvider.build)
        self.assertIn("min(2, settings.max_retries)", src)


if __name__ == "__main__":
    unittest.main()
