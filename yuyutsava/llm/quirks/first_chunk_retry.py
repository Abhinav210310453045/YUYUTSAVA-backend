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
import logging
import random
import time
from functools import lru_cache

logger = logging.getLogger("yuyutsava.llm.retry")

# Indirections so tests can patch the sleeps without touching asyncio/time.
_asleep = asyncio.sleep
_sleep = time.sleep

# Default backoff when the model carries no ``wait_exponential_kwargs``:
# 2, 4, 8, 16, 30, 30 … seconds (before jitter).
_DEFAULT_WAIT = {"multiplier": 2.0, "exp_base": 2.0, "min": 2.0, "max": 30.0}


def _is_transient(exc: BaseException) -> bool:
    """Google/gRPC errors that a fresh attempt can reasonably clear.

    Mirrors the set the Vertex SDK itself retries (``_utils.create_retry_decorator``)
    minus its catch-all ``GoogleAPIError``. Import failures mean "not transient" —
    the quirk must never be the thing that breaks a provider.
    """
    try:
        from google.api_core import exceptions as gexc
    except ImportError:  # pragma: no cover — provider extra not installed
        gexc = None
    if gexc is not None and isinstance(
        exc,
        (gexc.ResourceExhausted, gexc.ServiceUnavailable, gexc.Aborted, gexc.DeadlineExceeded),
    ):
        return True

    # Raw grpc.aio errors carry the status as a ``code()`` method.
    code = getattr(exc, "code", None)
    if callable(code):
        try:
            import grpc

            return code() in (grpc.StatusCode.RESOURCE_EXHAUSTED, grpc.StatusCode.UNAVAILABLE)
        except Exception:  # noqa: BLE001
            return False
    return False


def _max_retries(model: object) -> int:
    raw = getattr(model, "max_retries", 6)
    try:
        return max(0, int(raw))
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
        attempt = 0
        while True:
            agen = super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs)
            try:
                first = await agen.__anext__()
            except StopAsyncIteration:
                return
            except Exception as exc:  # noqa: BLE001 — filtered by _is_transient
                if attempt >= retries or not _is_transient(exc):
                    raise
                delay = _backoff(attempt, self)
                attempt += 1
                logger.warning(
                    "%s: %s before first chunk — retry %d/%d in %.1fs",
                    _label(self), type(exc).__name__, attempt, retries, delay,
                )
                await _asleep(delay)
                continue
            yield first
            async for chunk in agen:
                yield chunk
            return

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        retries = _max_retries(self)
        attempt = 0
        while True:
            gen = super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
            try:
                first = next(gen)
            except StopIteration:
                return
            except Exception as exc:  # noqa: BLE001 — filtered by _is_transient
                if attempt >= retries or not _is_transient(exc):
                    raise
                delay = _backoff(attempt, self)
                attempt += 1
                logger.warning(
                    "%s: %s before first chunk — retry %d/%d in %.1fs",
                    _label(self), type(exc).__name__, attempt, retries, delay,
                )
                _sleep(delay)
                continue
            yield first
            yield from gen
            return


@lru_cache(maxsize=None)
def first_chunk_retry(base: type) -> type:
    """``base`` re-based so a transient error before the first streamed chunk is retried.

    Cached for the same reason as the sibling quirks: one stable class object
    per base keeps ``isinstance``, pickling and identity intact.
    """
    return type(f"FirstChunkRetry{base.__name__}", (_FirstChunkRetryMixin, base), {})


__all__ = ["first_chunk_retry"]
