"""Retry a streamed model call when it fails *before the first chunk*.

The gap this closes — ``langchain_google_vertexai`` (and the GAPIC transport
under it) wrap only the call that *opens* a stream in their retry decorator.
With grpc.aio a unary-stream RPC opens successfully and the server's status
(429 RESOURCE_EXHAUSTED, 503 UNAVAILABLE …) surfaces on the first *read*,
inside ``async for`` — outside every retry layer. So ``max_retries=6`` is worth
zero on the streaming path, and one quota blip aborts a whole agent turn.

Retrying is only safe while nothing has been yielded: no token has reached the
renderer, so a fresh attempt cannot duplicate output. After the first chunk the
stream is handed through untouched.

Same shape as the other quirks (``gemini_parts.parts_safe``,
``loop_affinity.loop_pinned``): a mixin plus a cached class factory. Policy is
read off the model instance — ``max_retries`` and ``wait_exponential_kwargs``
are existing ``ChatVertexAI`` fields — so the mixin carries no state of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import time
from functools import lru_cache
from typing import Callable

from yuyutsava.config.env import env_float

logger = logging.getLogger("yuyutsava.llm.retry")

# Indirections so tests can patch the sleeps without touching asyncio/time.
_asleep = asyncio.sleep
_sleep = time.sleep
_now = time.monotonic

# Default backoff when the model carries no ``wait_exponential_kwargs``:
# 2, 4, 8, 16, 30, 30 … seconds (before jitter).
_DEFAULT_WAIT = {"multiplier": 2.0, "exp_base": 2.0, "min": 2.0, "max": 30.0}

# Wall-clock ceiling across ALL attempts for one call. ``max_retries`` alone
# bounds the count, not the time: six attempts on the default ladder is ~3.5
# minutes of a frozen spinner, and the provider's own retry decorator nests
# inside each of those. A user watching a terminal needs the turn to come back
# and say what happened.
_DEFAULT_BUDGET_SEC = 90.0


def _budget_sec() -> float:
    value = env_float("VERTEX_RETRY_BUDGET_SEC", default=_DEFAULT_BUDGET_SEC)
    return value if value > 0 else _DEFAULT_BUDGET_SEC


#: Called on every retry with ``(model, attempt, retries, delay, exc)``.
#: The terminal front registers one so a 429 storm shows up as spinner text
#: instead of a wall of provider log lines. Best-effort: a listener that raises
#: is logged and skipped, never propagated into a model call.
RetryListener = Callable[[str, int, int, float, BaseException], None]
_listeners: list[RetryListener] = []


def add_retry_listener(callback: RetryListener) -> Callable[[], None]:
    """Register *callback* for retry notices; returns the function that removes it.

    A list rather than one slot, so a second registration cannot silently
    replace the first, and each owner removes only its own.
    """
    _listeners.append(callback)

    def _remove() -> None:
        with contextlib.suppress(ValueError):
            _listeners.remove(callback)

    return _remove


def _notify(model: str, attempt: int, retries: int, delay: float, exc: BaseException) -> None:
    for cb in list(_listeners):
        try:
            cb(model, attempt, retries, delay, exc)
        except Exception:  # noqa: BLE001 — a broken listener must not fail the call
            logger.debug("retry listener raised", exc_info=True)


#: gRPC status names the raw ``grpc.aio`` errors report, as the HTTP code the
#: google-api-core wrappers carry for the same condition.
_GRPC_TO_HTTP = {
    "RESOURCE_EXHAUSTED": "429",
    "UNAVAILABLE": "503",
    "DEADLINE_EXCEEDED": "504",
    "ABORTED": "409",
}

#: The two capacity errors by class name, for errors that arrive without a
#: status — a stand-in, or an SDK that names them the same way.
_BUSY_BY_NAME = {"ResourceExhausted": "429", "ServiceUnavailable": "503"}


def classify_provider_busy(exc: BaseException) -> str:
    """The status to show a user for a provider error, e.g. ``"429"``.

    Read from the exception, never guessed from its class name. A name-based
    guess gets two things wrong: :func:`is_transient` also retries
    ``DeadlineExceeded`` and ``Aborted``, which the SDK reports as 504 and 409
    and a guess would announce as "provider busy (503)"; and a subclass either
    matches a name test or does not depending on whether it is a substring or
    exact test, so two surfaces can disagree about the same error.

    google-api-core exceptions carry the HTTP status as ``code``; raw
    ``grpc.aio`` errors carry a ``code()`` method returning a gRPC status; an
    error with neither is matched on its exact class name. Anything else reads
    ``"503"``.
    """
    code = getattr(exc, "code", None)
    if code is not None and not callable(code):
        try:
            return str(int(code))
        except (TypeError, ValueError):
            pass
    if callable(code):
        try:
            status = code()
        except Exception:  # noqa: BLE001 — a label must never raise over an error
            status = None
        name = getattr(status, "name", None)
        if name in _GRPC_TO_HTTP:
            return _GRPC_TO_HTTP[name]
    # Last resort, for an error that carries no status at all: its exact class
    # name. Exact, not a substring, and in this one place, so every surface
    # reaches the same answer for the same error.
    return _BUSY_BY_NAME.get(type(exc).__name__, "503")


#: HTTP statuses of a google-genai error that a fresh attempt can clear: quota
#: (429) and an unavailable or timed-out backend (503, 504) — the HTTP forms of
#: the gRPC statuses retried below.
_GENAI_BUSY = frozenset({429, 503, 504})


def _is_genai_error(exc: BaseException) -> bool:
    """An error from google-genai, which Gemini on Vertex and AI Studio both raise.

    Recognised by module rather than imported, so this module keeps no import of
    an optional SDK; its errors carry the HTTP status as an ``int`` ``code``.
    """
    return type(exc).__module__.startswith("google.genai") and isinstance(
        getattr(exc, "code", None), int
    )


def is_transient(exc: BaseException) -> bool:
    """Google/gRPC errors that a fresh attempt can reasonably clear.

    Mirrors the set the Vertex SDK itself retries (``_utils.create_retry_decorator``)
    minus its catch-all ``GoogleAPIError``. Import failures mean "not transient" —
    the quirk must never be the thing that breaks a provider.
    """
    try:
        from google.api_core import exceptions as gexc  # noqa: PLC0415 — optional dependency
    except ImportError:  # pragma: no cover — provider extra not installed
        gexc = None
    if gexc is not None and isinstance(
        exc,
        (gexc.ResourceExhausted, gexc.ServiceUnavailable, gexc.Aborted, gexc.DeadlineExceeded),
    ):
        return True

    # Raw grpc.aio errors carry the status as a ``code()`` method.
    code = getattr(exc, "code", None)
    if _is_genai_error(exc):
        return code in _GENAI_BUSY
    if callable(code):
        try:
            import grpc  # noqa: PLC0415 — optional dependency

            return code() in (grpc.StatusCode.RESOURCE_EXHAUSTED, grpc.StatusCode.UNAVAILABLE)
        except Exception:  # noqa: BLE001 — a classifier: "not busy" is the safe answer
            logger.debug(
                "classifying %s by its grpc code failed", type(exc).__name__, exc_info=True
            )
            return False
    return False


def _max_retries(model: object) -> int:
    """How many times *this* quirk retries — not what the SDK is set to.

    The provider pins the SDK's own ``max_retries`` low on purpose (see
    ``llm/providers/vertex.py``): its decorator covers the stream open, ours
    covers the first read, and at 6 each they multiplied into minutes of
    silence per call. So the user-facing knob is read here, where the retry
    that actually saves a turn happens, and the model field is only the
    fallback for a model built without it.
    """
    raw: object = os.environ.get("VERTEX_MAX_RETRIES", "").strip()
    if not raw:
        raw = getattr(model, "max_retries", 6)
    try:
        return max(0, int(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 6


def _backoff(attempt: int, model: object) -> float:
    """Exponential wait for ``attempt`` (0-based) with full jitter."""
    cfg = dict(_DEFAULT_WAIT)
    user = getattr(model, "wait_exponential_kwargs", None)
    if isinstance(user, dict):
        cfg.update({k: float(v) for k, v in user.items() if k in cfg})
    raw = cfg["multiplier"] * (cfg["exp_base"] ** attempt)
    bounded = min(max(raw, cfg["min"]), cfg["max"])
    return bounded * random.uniform(0.5, 1.0)


def _label(model: object) -> str:
    return getattr(model, "model_name", None) or type(model).__name__


class _FirstChunkRetryMixin:
    """Re-run ``_stream``/``_astream`` while the failure happens before any chunk."""

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        retries = _max_retries(self)
        budget = _budget_sec()
        started = _now()
        attempt = 0
        while True:
            agen = super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs)
            try:
                first = await agen.__anext__()
            except StopAsyncIteration:
                return
            except Exception as exc:  # noqa: BLE001 — filtered by is_transient
                delay = self._plan_retry(exc, attempt, retries, started, budget)
                if delay is None:
                    raise
                attempt += 1
                _notify(_label(self), attempt, retries, delay, exc)
                await _asleep(delay)
                continue
            yield first
            async for chunk in agen:
                yield chunk
            return

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        retries = _max_retries(self)
        budget = _budget_sec()
        started = _now()
        attempt = 0
        while True:
            gen = super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
            try:
                first = next(gen)
            except StopIteration:
                return
            except Exception as exc:  # noqa: BLE001 — filtered by is_transient
                delay = self._plan_retry(exc, attempt, retries, started, budget)
                if delay is None:
                    raise
                attempt += 1
                _notify(_label(self), attempt, retries, delay, exc)
                _sleep(delay)
                continue
            yield first
            yield from gen
            return

    def _plan_retry(
        self,
        exc: BaseException,
        attempt: int,
        retries: int,
        started: float,
        budget: float,
    ) -> float | None:
        """Seconds to wait before the next attempt, or ``None`` to give up.

        Gives up on a non-transient error, on the attempt count, and — the
        addition — when the next wait would run past the wall-clock budget.
        Stopping at the budget is what turns a quota storm into one honest
        error the caller can report instead of a spinner that never returns.
        """
        if not is_transient(exc):
            return None
        label = _label(self)
        if attempt >= retries:
            logger.warning(
                "%s: %s before first chunk — giving up after %d attempt(s)",
                label,
                type(exc).__name__,
                attempt,
            )
            return None
        delay = _backoff(attempt, self)
        elapsed = _now() - started
        if elapsed + delay > budget:
            logger.warning(
                "%s: %s before first chunk — giving up after %.0fs "
                "(VERTEX_RETRY_BUDGET_SEC=%.0f); the provider is still busy",
                label,
                type(exc).__name__,
                elapsed,
                budget,
            )
            return None
        logger.warning(
            "%s: %s before first chunk — retry %d/%d in %.1fs",
            label,
            type(exc).__name__,
            attempt + 1,
            retries,
            delay,
        )
        return delay


@lru_cache(maxsize=None)
def first_chunk_retry(base: type) -> type:
    """``base`` re-based so a transient error before the first streamed chunk is retried.

    Cached for the same reason as the sibling quirks: one stable class object
    per base keeps ``isinstance``, pickling and identity intact.
    """
    return type(f"FirstChunkRetry{base.__name__}", (_FirstChunkRetryMixin, base), {})


__all__ = ["first_chunk_retry", "add_retry_listener", "RetryListener"]
