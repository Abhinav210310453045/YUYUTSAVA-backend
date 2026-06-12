"""FastAPI app factory.

Wires routers, attaches daemon singletons to ``app.state`` for ``Depends``,
registers exception handlers, and exposes Swagger UI / ReDoc at ``/docs``
and ``/redoc``.

Bind policy (Phase 2): loopback binds stay unauthenticated (the Electron
renderer is single-user and local). Non-loopback binds — e.g. a Tailscale
address for the mobile app — are allowed **iff** bearer-token auth is
active; the factory refuses to build an unauthenticated network-exposed
app.
"""

from __future__ import annotations

import os
import time
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from yuyutsava.daemon.web.auth import (
    AuthSettings,
    install_auth_middleware,
    is_loopback_host,
)
from yuyutsava.daemon.web.exceptions import register_exception_handlers
from yuyutsava.daemon.web.routers import (
    channels as channels_router,
    cli_attach as cli_attach_router,
    config as config_router,
    db as db_router,
    decisions as decisions_router,
    health as health_router,
    logs as logs_router,
    proposals as proposals_router,
    rules as rules_router,
    sessions as sessions_router,
    skills as skills_router,
    static_files as static_router,
    stream as stream_router,
    system as system_router,
    tasks as tasks_router,
    usage as usage_router,
)
from yuyutsava.daemon.channels import HttpLogPayload
from yuyutsava.daemon.web.services.decision_service import DecisionService
from yuyutsava.daemon.web.services.stream_service import StreamEventItem, WebHub
from yuyutsava.skills.registry import SkillRegistry


ReloadCallback = Callable[[], Awaitable[None]] | None


def _cors_kwargs() -> dict:
    """Explicit origins from ``YUYUTSAVA_CORS_ORIGINS`` (comma-separated),
    falling back to the historical loopback-only regex."""
    raw = os.environ.get("YUYUTSAVA_CORS_ORIGINS", "").strip()
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if origins:
        return {"allow_origins": origins}
    return {"allow_origin_regex": r"http://(localhost|127\.0\.0\.1)(:\d+)?"}


def create_app(
    hub: WebHub,
    *,
    host: str,
    skill_registry: SkillRegistry | None = None,
    config_reload: ReloadCallback = None,
    channels: "object | None" = None,           # ChannelRouter; duck-typed
    session_origin: "object | None" = None,     # SessionOriginMap; duck-typed
    auth: AuthSettings | None = None,
    task_registry: "object | None" = None,      # TaskRegistry; duck-typed
    task_submission: "object | None" = None,    # TaskSubmissionService; duck-typed
    decision_service: "object | None" = None,   # DecisionService; duck-typed
    channel_plugins: "object | None" = None,    # ChannelPluginRegistry; duck-typed
    usage_store: "object | None" = None,        # daemon.usage.UsageStore; duck-typed
    resource_monitor: "object | None" = None,   # daemon.resources.ResourceMonitor; duck-typed
    admission_controller: "object | None" = None,  # daemon.resources.AdmissionController; duck-typed
) -> FastAPI:
    if auth is None:
        auth = AuthSettings.from_env(host=host)
    if not is_loopback_host(host) and not (auth.enforce and auth.token):
        raise RuntimeError(
            f"Refusing to bind to non-loopback host {host!r} without bearer "
            "auth. Set YUYUTSAVA_API_TOKEN (or let it auto-generate to "
            "~/.yuyutsava/api_token) — the API must never be network-exposed "
            "unauthenticated."
        )

    app = FastAPI(
        title="YUYUTSAVA daemon",
        version="0.2.0",
        description=(
            "HTTP API for the YUYUTSAVA daemon. Proposals, consent "
            "rules, decisions, skills, tasks, and live event stream. "
            "Loopback binds are unauthenticated; network binds require "
            "'Authorization: Bearer <token>'."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # When the daemon didn't pass a shared DecisionService (tests, embedded
    # use), build a hub-local one so the proposal/ask endpoints keep working
    # exactly as before the Phase-3 extraction.
    if decision_service is None:
        decision_service = DecisionService(hub.store)
        decision_service.add_waiters(
            proposals=hub.pending_proposals, asks=hub.pending_asks,
        )

    # State for Depends(...) accessors.
    app.state.hub = hub
    app.state.store = hub.store
    app.state.skill_registry = skill_registry
    app.state.config_reload = config_reload
    app.state.channels = channels
    app.state.session_origin = session_origin
    app.state.task_registry = task_registry
    app.state.task_submission = task_submission
    app.state.decision_service = decision_service
    app.state.channel_plugins = channel_plugins
    app.state.usage_store = usage_store
    app.state.resource_monitor = resource_monitor
    app.state.admission_controller = admission_controller

    # Auth first so CORSMiddleware (added after → wraps outside) answers
    # preflight OPTIONS before the bearer check can 401 them.
    install_auth_middleware(app, auth)

    app.add_middleware(
        CORSMiddleware,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        **_cors_kwargs(),
    )

    register_exception_handlers(app)

    @app.middleware("http")
    async def _broadcast_http_log(request: Request, call_next):
        # NOTE: only ``request.url.path`` is logged — never the query string,
        # which may carry ``?token=`` on /stream.
        path = request.url.path
        start = time.perf_counter()
        response = await call_next(request)
        # Suppress feedback loop: /stream subscribers would receive their own
        # request event. Static assets are noisy and not interesting.
        if path == "/stream" or path.startswith("/static"):
            return response
        duration_ms = int((time.perf_counter() - start) * 1000)
        try:
            await hub.broadcast(StreamEventItem(payload=HttpLogPayload(
                method=request.method,
                path=path,
                status=response.status_code,
                duration_ms=duration_ms,
                ts=time.time(),
            )))
        except Exception:
            # Broadcasting must never break a request.
            pass
        return response

    for r in (
        health_router.router,
        stream_router.router,
        proposals_router.router,
        rules_router.router,
        decisions_router.router,
        sessions_router.router,
        skills_router.router,
        config_router.router,
        logs_router.router,
        static_router.router,
        cli_attach_router.router,
        tasks_router.router,
        channels_router.router,
        usage_router.router,
        system_router.router,
    ):
        app.include_router(r)

    # Read-only DB introspection. Opt-out via env (defaults on).
    if os.environ.get("YUYUTSAVA_DB_API_ENABLED", "true").lower() not in {"0", "false", "no"}:
        app.include_router(db_router.router)

    return app
