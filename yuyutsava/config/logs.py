"""Keeping third-party plumbing loggers quiet, for the CLI and the daemon alike.

:func:`silence_plumbing_loggers` raises chatty HTTP and runtime loggers (the
LangGraph dev server, uvicorn access lines, httpx, the OpenTelemetry exporter) to
WARNING or ERROR and gives them a null handler, so interactive output is not
flooded. Both front ends call it; each sets up its own handlers separately.
"""

from __future__ import annotations

import logging
import os
import warnings

# Loggers raised to WARNING by silence_plumbing_loggers — these emit chatty
# HTTP / runtime traffic (langgraph dev server, uvicorn access lines,
# httpx client logs) that pollute interactive output. Real problems still
# surface at WARNING/ERROR.
_PLUMBING_WARN_LOGGERS = (
    "langgraph_api",
    "langgraph_runtime_inmem",
    "langgraph_runtime",
    "langgraph_api.auth.custom",
    "langgraph_api.auth.middleware",
    "langgraph_api.timing.timer",
    "langgraph_api.cron_scheduler",
    "langgraph_api.metadata",
    "langgraph_api.lifespan",
    "langgraph_api.queue",
    "langgraph_runtime_inmem.queue",
    "langgraph_runtime_inmem.lifespan",
    "langgraph_runtime_inmem._persistence",
    "httpx",
    "httpcore",
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
)

# Loggers raised to ERROR — these emit chatty WARNING-level lines that
# aren't actionable for interactive users (langfuse handshake, warnings hook).
_PLUMBING_ERROR_LOGGERS = (
    "langfuse",
    # OTEL exporter retry spam when Langfuse drops mid-session (the whole
    # opentelemetry.* tree, incl. exporter.otlp.proto.http.trace_exporter).
    "opentelemetry",
    "py.warnings",
    # Vertex re-converts every tool schema on each bind and warns once per
    # `additionalProperties` key Pydantic emitted — pure noise, N× per turn.
    "langchain_google_vertexai.functions_utils",
)


def _ensure_null_handler(lg: logging.Logger) -> None:
    """Give a silenced logger a handler so its *children* stay silent too.

    ``propagate = False`` on a parent stops records reaching the root handlers,
    and ``disabled = True`` only applies to records emitted on that exact
    logger. A child (``opentelemetry.exporter.otlp.proto.http.trace_exporter``
    logging at ERROR) walks up to the parent, finds *no* handler anywhere, and
    Python then falls back to ``logging.lastResort`` — a bare stderr print. That
    is how "Failed to export span batch code: 404" reached the terminal despite
    the silencing. A ``NullHandler`` makes the search succeed and swallow.
    """
    if not any(isinstance(h, logging.NullHandler) for h in lg.handlers):
        lg.addHandler(logging.NullHandler())


def silence_plumbing_loggers() -> None:
    """Raise plumbing loggers above the chatter floor.

    The ``yuyutsava`` namespace is left untouched — application warnings
    still surface. Cross-call idempotent; ``langgraph_api`` re-imports
    ``logging`` inside its server thread, so callers that build a LangGraph
    host should call this *after* the build to re-silence handlers that
    were just installed.
    """

    for name in _PLUMBING_WARN_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        lg.propagate = False
        _ensure_null_handler(lg)
    for name in _PLUMBING_ERROR_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(logging.ERROR)
        lg.propagate = False
        lg.disabled = True
        _ensure_null_handler(lg)
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=PendingDeprecationWarning)


def quiet_grpc_fork_logs() -> None:
    """Stop gRPC's C core logging over the renderer and the daemon log.

    It logs at INFO by default, and its fork handlers fire on every subprocess
    the task runner spawns — so each ``tr_run_python`` / ``tr_execute``
    printed ``fork_posix.cc … Other threads are currently calling into gRPC``
    and ``ev_poll_posix.cc … FD from fork parent still in poll list`` straight
    over whatever was on screen.

    **When this is called matters.** gRPC reads the variable when it is first
    imported, which happens when a model is built — so this only works while
    importing an entry point does not itself import gRPC.
    ``test/cli/test_grpc_env_order.py`` checks exactly that, in a fresh
    interpreter, because the test process may already have gRPC loaded.

    ``setdefault``, so an operator debugging gRPC can still override it.
    """
    os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
