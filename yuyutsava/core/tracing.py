"""
LangFuse tracing integration (langfuse v4+).

Returns a fresh CallbackHandler when LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY,
and LANGFUSE_SECRET_KEY are all set in the environment; otherwise returns
None so tracing is a no-op.  Errors are silently swallowed — tracing must
never break agent execution.

Usage::

    from yuyutsava.core.tracing import get_callback

    cb = get_callback(session_id=thread_id, trace_name="orchestrator")
    if cb:
        cfg["callbacks"] = [cb]
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("yuyutsava.core.tracing")


def is_configured() -> bool:
    return bool(
        os.getenv("LANGFUSE_HOST")
        and os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_SECRET_KEY")
    )


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
