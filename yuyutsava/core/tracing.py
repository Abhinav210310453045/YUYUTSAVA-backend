"""
LangFuse tracing integration (langfuse v4+).

Returns a fresh CallbackHandler when LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY,
and LANGFUSE_SECRET_KEY are all set in the environment; otherwise returns
None so tracing is a no-op.  Errors are silently swallowed — tracing must
never break agent execution.

A single ``LANGFUSE_ENABLED`` kill-switch overrides everything: set it to an
explicit off value (``0``/``false``/``no``/``off``) to force tracing off even
when the keys are present and the server is up — the in-code twin of *not*
starting the ``langfuse`` compose profile. Leaving it unset preserves the
historical behaviour (active iff the three keys are set).

Usage::

    from yuyutsava.core.tracing import get_callback

    cb = get_callback(session_id=thread_id, trace_name="orchestrator")
    if cb:
        cfg["callbacks"] = [cb]
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("yuyutsava.core.tracing")

# Process-wide cache for the reachability probe. ``None`` = not yet probed.
_reachable: bool | None = None


def _explicitly_disabled() -> bool:
    """True only when ``LANGFUSE_ENABLED`` is set to an explicit off value.

    Unset returns False so existing setups (keys present, no flag) keep tracing
    on without touching their env.
    """
    raw = os.getenv("LANGFUSE_ENABLED")
    if raw is None:
        return False
    return raw.strip().lower() in ("0", "false", "no", "off", "")


def is_configured() -> bool:
    if _explicitly_disabled():
        return False
    return bool(
        os.getenv("LANGFUSE_HOST")
        and os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_SECRET_KEY")
    )


def reset_reachability_cache() -> None:
    """Clear the cached reachability result (mainly for tests)."""
    global _reachable
    _reachable = None


def warm_reachability_cache() -> None:
    """Probe Langfuse once at startup so the runtime path never blocks.

    ``_langfuse_reachable`` does a synchronous ``urllib`` call. Calling it once
    during boot (before the LangGraph runtime installs blockbuster) populates the
    process-wide cache, so the later on-loop ``get_callback`` calls reuse it
    instead of doing socket I/O on the event loop. No-op unless Langfuse is
    configured (otherwise the probe is never reached).
    """
    if is_configured():
        _langfuse_reachable()


def _langfuse_reachable() -> bool:
    """Return whether Langfuse is actually up, probing once per process.

    Without this, langfuse v4 happily installs a global OTEL ``BatchSpanProcessor``
    aimed at a dead ``LANGFUSE_HOST`` and the exporter spams retry warnings. We
    probe ``/api/public/health`` once, cache the result, and log a single quiet
    line when Langfuse is unreachable so tracing degrades to a silent no-op.
    """
    global _reachable
    if _reachable is not None:
        return _reachable

    host = (os.getenv("LANGFUSE_HOST") or "").rstrip("/")
    ok = False
    try:
        with urllib.request.urlopen(f"{host}/api/public/health", timeout=1.5) as r:
            ok = r.status == 200
    except (urllib.error.URLError, ConnectionError, OSError, ValueError):
        ok = False

    _reachable = ok
    if not ok:
        logger.info("Langfuse not active at %s — tracing disabled", host or "<unset>")
    return ok


def get_callback(
    *,
    session_id: str | None = None,
    trace_name: str | None = None,
    run_name: str | None = None,  # alias — callers may pass either
):
    """Return a LangFuse CallbackHandler, or None if not configured / not installed.

    In langfuse v4 credentials are read from LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY /
    LANGFUSE_HOST env vars.  Session and trace name go via TraceContext.
    """
    if not is_configured():
        return None
    if not _langfuse_reachable():
        return None
    name = trace_name or run_name
    try:
        from langfuse.langchain import CallbackHandler
        from langfuse.types import TraceContext

        ctx: dict = {}
        if session_id:
            ctx["session_id"] = session_id
        if name:
            ctx["trace_name"] = name

        return CallbackHandler(trace_context=TraceContext(**ctx) if ctx else None)
    except ImportError:
        logger.warning("langfuse is not installed — tracing disabled. Run: uv add langfuse")
        return None
    except Exception as exc:
        logger.warning("LangFuse callback init failed (%s) — tracing disabled", exc)
        return None
