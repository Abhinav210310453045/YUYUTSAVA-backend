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

Session id, trace name and tags are *not* set on the handler in langfuse v4 — the
LangChain ``CallbackHandler`` reads them off the root run's ``metadata`` under the
``langfuse_session_id`` / ``langfuse_trace_name`` / ``langfuse_tags`` keys. Use
``trace_metadata`` to build that block and merge it into the RunnableConfig.

Usage::

    from yuyutsava.core.tracing import get_callback, trace_metadata

    cb = get_callback()
    if cb:
        cfg["callbacks"] = [cb]
        cfg["metadata"] = {
            **(cfg.get("metadata") or {}),
            **trace_metadata(session_id=thread_id, trace_name="orchestrator"),
        }
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("yuyutsava.core.tracing")

# Process-wide cache for the reachability probe. ``None`` = not yet probed.
_reachable: bool | None = None

# Dead switch: flipped by the exporter the first time a span batch cannot be
# delivered (Langfuse stopped mid-session, or something else took its port).
# Once dead, ``get_callback`` hands out nothing and the exporter drops whatever
# is still queued — no retries, no log spam, no requests at a stranger's port.
# Dead stays dead for the process; Langfuse is opt-in, not a thing to chase.
_dead: bool = False

# The one Langfuse client this process owns (built lazily with our exporter).
_client = None


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
    """Clear the cached reachability result and the dead switch (mainly for tests).

    Does not tear down an already-built Langfuse client — its resource manager
    is a per-key singleton inside the SDK; tests that need a fresh one call
    ``LangfuseResourceManager.reset()`` themselves.
    """
    global _reachable, _dead, _client
    _reachable = None
    _dead = False
    _client = None


def is_dead() -> bool:
    """True once a span export has failed in this process (tracing switched off)."""
    return _dead


def _mark_dead(reason: str) -> None:
    """Flip the dead switch exactly once, with a single quiet INFO line."""
    global _reachable, _dead
    if _dead:
        return
    _dead = True
    _reachable = False
    host = (os.getenv("LANGFUSE_HOST") or "").rstrip("/")
    logger.info(
        "Langfuse unreachable at %s (%s) — tracing disabled for this process",
        host or "<unset>",
        reason,
    )


def _dead_switch_exporter_class():
    """Build (once) the exporter subclass; imports are lazy so this module
    stays importable when langfuse/opentelemetry are not installed."""
    global _ExporterClass
    if _ExporterClass is not None:
        return _ExporterClass

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SpanExportResult

    class _DeadSwitchExporter(OTLPSpanExporter):
        """OTLP exporter that gives up for good on the first failed batch.

        The stock exporter retries transient failures with backoff and logs
        every non-retryable status at ERROR, forever, batch after batch. We let
        it try once (its own retry loop is bounded by the export timeout); if
        the batch does not land, tracing is over for this process and every
        later batch is discarded without touching the network.
        """

        def export(self, spans):  # type: ignore[override]
            if _dead:
                return SpanExportResult.SUCCESS
            try:
                result = super().export(spans)
            except Exception as exc:  # noqa: BLE001 — never let tracing raise
                _mark_dead(f"{type(exc).__name__}: {exc}")
                return SpanExportResult.SUCCESS
            if result is SpanExportResult.SUCCESS:
                return result
            _mark_dead("span export failed")
            return SpanExportResult.SUCCESS

    _ExporterClass = _DeadSwitchExporter
    return _ExporterClass


_ExporterClass = None


def _build_exporter():
    """Our exporter, wired exactly as langfuse's ``LangfuseSpanProcessor`` would
    wire its default one (endpoint, Basic auth, SDK headers, timeout)."""
    import base64
    from importlib.metadata import PackageNotFoundError, version

    host = (os.getenv("LANGFUSE_HOST") or "").rstrip("/")
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY") or ""
    secret_key = os.getenv("LANGFUSE_SECRET_KEY") or ""
    try:
        sdk_version = version("langfuse")
    except PackageNotFoundError:
        sdk_version = "unknown"
    try:
        timeout = int(os.getenv("LANGFUSE_TIMEOUT") or 5)
    except ValueError:
        timeout = 5

    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    headers = {
        "Authorization": f"Basic {auth}",
        "x-langfuse-sdk-name": "python",
        "x-langfuse-sdk-version": sdk_version,
        "x-langfuse-public-key": public_key,
    }
    export_path = os.getenv("LANGFUSE_OTEL_TRACES_EXPORT_PATH")
    endpoint = f"{host}/{export_path}" if export_path else f"{host}/api/public/otel/v1/traces"
    cls = _dead_switch_exporter_class()
    return cls(endpoint=endpoint, headers=headers, timeout=timeout)


def _ensure_client():
    """Construct the process's Langfuse client once, with the dead-switch exporter.

    The SDK's ``LangfuseResourceManager`` is a per-public-key singleton, so a
    client built here first is the one every later ``CallbackHandler()`` /
    ``get_client()`` reuses — which is what makes the injected exporter stick.
    """
    global _client
    if _client is not None:
        return _client
    from langfuse import Langfuse

    _client = Langfuse(span_exporter=_build_exporter())
    return _client


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


def get_callback():
    """Return a LangFuse CallbackHandler, or None if not configured / not installed.

    In langfuse v4 credentials are read from LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY /
    LANGFUSE_HOST env vars. The handler carries no per-run state — session id, trace
    name and tags are passed through the RunnableConfig ``metadata`` (see
    ``trace_metadata``).
    """
    if not is_configured():
        return None
    if _dead or not _langfuse_reachable():
        return None
    try:
        from langfuse.langchain import CallbackHandler

        _ensure_client()
        return CallbackHandler()
    except ImportError:
        logger.warning("langfuse is not installed — tracing disabled. Run: uv add langfuse")
        return None
    except Exception as exc:
        logger.warning("LangFuse callback init failed (%s) — tracing disabled", exc)
        return None


def trace_metadata(
    *,
    session_id: str | None = None,
    trace_name: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Langfuse v4 trace attributes for a RunnableConfig ``metadata`` dict.

    In langfuse v4 these are read off the root run's metadata by the LangChain
    CallbackHandler (keys: ``langfuse_session_id`` / ``langfuse_trace_name`` /
    ``langfuse_tags``). Returns only the keys that have values, so it is safe to
    splat into a metadata dict unconditionally.
    """
    md: dict = {}
    if session_id:
        md["langfuse_session_id"] = session_id
    if trace_name:
        md["langfuse_trace_name"] = trace_name
    if tags:
        md["langfuse_tags"] = tags
    return md
