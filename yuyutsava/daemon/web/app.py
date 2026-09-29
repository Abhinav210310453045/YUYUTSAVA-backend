"""FastAPI app factory.

Wires routers, attaches daemon singletons to ``app.state`` for ``Depends``,
registers exception handlers, and exposes Swagger UI / ReDoc at ``/docs``
and ``/redoc``.

Bind policy: loopback binds stay unauthenticated (the Electron
renderer is single-user and local). Non-loopback binds — e.g. a Tailscale
address for the mobile app — are allowed **iff** bearer-token auth is
active; the factory refuses to build an unauthenticated network-exposed
app.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from yuyutsava.config.env import env_flag
from yuyutsava.daemon.web.app_deps import AppDeps
from yuyutsava.daemon.web.auth import (
    AuthSettings,
    install_auth_middleware,
    is_loopback_host,
)
from yuyutsava.daemon.web.exceptions import register_exception_handlers
from yuyutsava.daemon.web.routers import (
    artifacts as artifacts_router,
)
from yuyutsava.daemon.web.routers import (
    channels as channels_router,
)
from yuyutsava.daemon.web.routers import (
    cli_attach as cli_attach_router,
)
from yuyutsava.daemon.web.routers import (
    config as config_router,
)
from yuyutsava.daemon.web.routers import (
    converse as converse_router,
)
from yuyutsava.daemon.web.routers import (
    db as db_router,
)
from yuyutsava.daemon.web.routers import (
    decisions as decisions_router,
)
from yuyutsava.daemon.web.routers import (
    feedback as feedback_router,
)
from yuyutsava.daemon.web.routers import (
    health as health_router,
)
from yuyutsava.daemon.web.routers import (
    logs as logs_router,
)
from yuyutsava.daemon.web.routers import (
    mcp as mcp_router,
)
from yuyutsava.daemon.web.routers import (
    proposals as proposals_router,
)
from yuyutsava.daemon.web.routers import (
    rules as rules_router,
)
from yuyutsava.daemon.web.routers import (
    server_info as server_info_router,
)
from yuyutsava.daemon.web.routers import (
    sessions as sessions_router,
)
from yuyutsava.daemon.web.routers import (
    settings as settings_router,
)
from yuyutsava.daemon.web.routers import (
    skills as skills_router,
)
from yuyutsava.daemon.web.routers import (
    static_files as static_router,
)
from yuyutsava.daemon.web.routers import (
    stream as stream_router,
)
from yuyutsava.daemon.web.routers import (
    system as system_router,
)
from yuyutsava.daemon.web.routers import (
    tasks as tasks_router,
)
from yuyutsava.daemon.web.routers import (
    todos as todos_router,
)
from yuyutsava.daemon.web.routers import (
    usage as usage_router,
)
from yuyutsava.daemon.web.routers import (
    visuals as visuals_router,
)
from yuyutsava.daemon.web.services.decision_service import DecisionService
from yuyutsava.daemon.web.services.stream_service import StreamEventItem, WebHub
from yuyutsava.ports.channels import HttpLogPayload

logger = logging.getLogger("yuyutsava.daemon.web.app")

ReloadCallback = Callable[[], Awaitable[None]] | None


def _cors_kwargs() -> dict:
    """Explicit origins from ``YUYUTSAVA_CORS_ORIGINS`` (comma-separated),
    falling back to the historical loopback-only regex."""
    raw = os.environ.get("YUYUTSAVA_CORS_ORIGINS", "").strip()
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if origins:
        return {"allow_origins": origins}
    return {"allow_origin_regex": r"http://(localhost|127\.0\.0\.1)(:\d+)?"}


def _checked_auth(host: str, auth: AuthSettings | None) -> AuthSettings:
    """The auth settings to enforce, refusing an unauthenticated network bind."""
    if auth is None:
        auth = AuthSettings.from_env(host=host)
    if not is_loopback_host(host) and not (auth.enforce and auth.token):
        raise RuntimeError(
            f"Refusing to bind to non-loopback host {host!r} without bearer "
            "auth. Set YUYUTSAVA_API_TOKEN (or let it auto-generate to "
            "~/.yuyutsava/api_token) — the API must never be network-exposed "
            "unauthenticated."
        )
    return auth


def _install_http_log(app: FastAPI, hub: WebHub) -> None:
    """Broadcast one ``HttpLogPayload`` per request to the live stream."""

    @app.middleware("http")
    async def _broadcast_http_log(request: Request, call_next):
        # NOTE: only ``request.url.path`` is logged — never the query string,
        # which may carry ``?token=`` on /stream.
        path = request.url.path
        start = time.perf_counter()
        response = await call_next(request)
        # Suppress feedback loop: /stream subscribers would receive their own
        # request event. Static assets are noisy and not interesting.
        if path in ("/stream", "/v1/stream") or path.startswith("/static"):
            return response
        duration_ms = int((time.perf_counter() - start) * 1000)
        try:
            await hub.broadcast(
                StreamEventItem(
                    payload=HttpLogPayload(
                        method=request.method,
                        path=path,
                        status=response.status_code,
                        duration_ms=duration_ms,
                        ts=time.time(),
                    )
                )
            )
        except Exception:  # noqa: BLE001 — broadcasting must never break a request
            logger.debug("broadcasting the http log line failed", exc_info=True)
        return response


def _mount_routers(app: FastAPI) -> None:
    """Mount every API router twice, and the static assets once.

    Canonical under /v1 (what /openapi.json documents; the mobile TS client
    generates from it) and unprefixed as a legacy alias (hidden from the
    schema) so the Electron renderer keeps working untouched. Static assets
    stay unprefixed only.
    """
    api_routers = [
        health_router.router,
        server_info_router.router,
        stream_router.router,
        proposals_router.router,
        rules_router.router,
        decisions_router.router,
        sessions_router.router,
        skills_router.router,
        config_router.router,
        settings_router.router,
        logs_router.router,
        cli_attach_router.router,
        tasks_router.router,
        channels_router.router,
        usage_router.router,
        system_router.router,
        converse_router.router,
        visuals_router.router,
        feedback_router.router,
        todos_router.router,
        artifacts_router.router,
        mcp_router.router,
    ]
    # Read-only DB introspection. Opt-out via env (defaults on).
    if env_flag("YUYUTSAVA_DB_API_ENABLED", default=True, false=("0", "false", "no")):
        api_routers.append(db_router.router)

    for r in api_routers:
        app.include_router(r, prefix="/v1")
        app.include_router(r, include_in_schema=False)

    app.include_router(static_router.router)


def create_app(
    hub: WebHub,
    *,
    host: str,
    deps: AppDeps | None = None,
) -> FastAPI:
    """The daemon's HTTP API over *hub*, with what the routers read on ``app.state``.

    Every :class:`AppDeps` field lands on ``app.state`` under its own name,
    where the ``Depends(...)`` accessors find it — except ``auth``, which only
    the middleware reads. A non-loopback *host* without bearer auth is refused.
    """
    deps = deps or AppDeps()
    auth = _checked_auth(host, deps.auth)

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

    app.state.hub = hub
    app.state.store = hub.store
    for field in dataclasses.fields(deps):
        if field.name != "auth":
            setattr(app.state, field.name, getattr(deps, field.name))
    # Without a shared DecisionService (tests, embedded use), a hub-local one
    # keeps the proposal/ask endpoints working.
    if deps.decision_service is None:
        decision_service = DecisionService(hub.store)
        decision_service.add_waiters(
            proposals=hub.pending_proposals,
            asks=hub.pending_asks,
        )
        app.state.decision_service = decision_service

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
    _install_http_log(app, hub)
    _mount_routers(app)
    return app
