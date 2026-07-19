"""Event-loop pinning for loop-affine model SDKs — fail fast, fail readable.

## The failure

Both Gemini SDKs lazily cache an async client on first use — ``ChatVertexAI``
a grpc.aio ``PredictionServiceAsyncClient`` (``langchain_google_vertexai/
_base.py``), ``ChatGoogleGenerativeAI`` its google-genai equivalent — and that
client binds permanently to the event loop running at creation time. A model
instance shared between two loops (the daemon main loop and the
AsyncSubagentHost's uvicorn loop are both live in this process) works on the
first loop and then crashes on the second, mid-request, with the opaque::

    RuntimeError: Task <...> got Future <...> attached to a different loop

The policy is therefore **one model instance per event loop**: every
``_build_host`` factory constructs its own ``chat_model(...)`` for the host
graphs instead of borrowing the main loop's (see Architecture.md,
"Event-loop ownership"). This quirk enforces the policy: the first async call
pins the instance to its loop, and any later call from a *different live* loop
raises immediately with a message that names the instance and the fix — before
a request is half-sent, instead of the grpc stack trace above.

## Why guard, not transparently swap clients per loop

A per-loop client cache inside a shared instance looks friendlier but is racy:
the SDK reads ``self.async_client`` *after* awaits inside its request path, so
two loops swapping the attribute under each other can still hand a client to
the wrong loop. Ownership separation is the correct fix; the guard just makes
violations loud.

## The one forgiving case

A pin to a loop that is dead (closed or collected) re-pins to the caller's
loop and drops the SDK's cached async client so a fresh one binds here. That
keeps sequential ``asyncio.run()`` callers — CLI one-shots, scripts, tests —
working without ceremony.

Applied like ``parts_safe`` (see ``gemini_parts.py``): only by the two Gemini
providers; the httpx-based providers create connections per request against
the running loop and don't need it.
"""

from __future__ import annotations

import asyncio
import weakref
from functools import lru_cache

# Attribute set via object.__setattr__ — bypasses pydantic v2 field validation
# and lands in the instance __dict__, so model_copy() carries the pin along
# with the (shared) cached client, which is the correct pairing.
_PIN_ATTR = "_yy_home_loop"

# SDK attribute names that may hold a loop-bound cached async client; reset
# best-effort on dead-loop re-pin so a fresh client binds to the new loop.
_ASYNC_CLIENT_ATTRS = ("async_client",)


class _LoopPinnedMixin:
    """Pins the instance to the first loop that drives it asynchronously."""

    def _yy_check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        pin: weakref.ref | None = getattr(self, _PIN_ATTR, None)
        home = pin() if pin is not None else None
        if home is None or home.is_closed():
            if home is not None:
                # Re-pin after a dead loop: the cached client died with it.
                for attr in _ASYNC_CLIENT_ATTRS:
                    if getattr(self, attr, None) is not None:
                        try:
                            setattr(self, attr, None)
                        except Exception:  # noqa: BLE001 - best-effort reset
                            pass
            object.__setattr__(self, _PIN_ATTR, weakref.ref(loop))
            return
        if home is not loop:
            raise RuntimeError(
                f"{type(self).__name__} instance is pinned to another event loop. "
                "Its SDK caches an async client bound to the first loop that used "
                "it, so one instance must never serve two loops (this process "
                "runs the main loop plus the async-subagent-host loop). Build a "
                "separate chat_model() per event loop — see Architecture.md "
                "'Event-loop ownership'."
            )

    async def _agenerate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._yy_check_loop()
        return await super()._agenerate(*args, **kwargs)

    async def _astream(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._yy_check_loop()
        async for chunk in super()._astream(*args, **kwargs):
            yield chunk


@lru_cache(maxsize=None)
def loop_pinned(base: type) -> type:
    """``base`` re-based so cross-loop use fails fast with an actionable error.

    Cached for the same reason as ``parts_safe``: provider SDKs are lazily
    imported, so the subclass can't exist at import time, and a stable class
    object per base keeps ``isinstance``/pickling/identity intact. Composes
    with other quirks: ``loop_pinned(parts_safe(Base))``.
    """
    return type(f"LoopPinned{base.__name__}", (_LoopPinnedMixin, base), {})


__all__ = ["loop_pinned"]
