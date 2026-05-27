"""FastAPI app factory.

Wires routers, attaches daemon singletons to ``app.state`` for ``Depends``,
registers exception handlers, and exposes Swagger UI / ReDoc at ``/docs``
and ``/redoc``.

The server is loopback-only by contract — refusing to bind to a non-loopback
host avoids accidentally exposing an unauthenticated agent to the LAN.
"""

from __future__ import annotations

import os
import time
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from yuyutsava.daemon.web.exceptions import register_exception_handlers
from yuyutsava.daemon.web.routers import (
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
)
from yuyutsava.daemon.channels import HttpLogPayload
from yuyutsava.daemon.web.services.stream_service import StreamEventItem, WebHub
from yuyutsava.skills.registry import SkillRegistry


ReloadCallback = Callable[[], Awaitable[None]] | None


def create_app(
    hub: WebHub,
    *,
    host: str,
    skill_registry: SkillRegistry | None = None,
    config_reload: ReloadCallback = None,
    channels: "object | None" = None,           # ChannelRouter; duck-typed
    session_origin: "object | None" = None,     # SessionOriginMap; duck-typed
) -> FastAPI:
    if not (host.startswith("127.") or host == "localhost" or host == "::1"):
        raise RuntimeError(
            f"Refusing to bind to non-loopback host {host!r}. "
            "The web window is single-user and not authenticated for network access."
        )

    app = FastAPI(
        title="YUYUTSAVA daemon",
        version="0.2.0",
        description=(
            "Local-only HTTP API for the YUYUTSAVA daemon. Proposals, consent "
            "rules, decisions, skills, and live event stream."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # State for Depends(...) accessors.
    app.state.hub = hub
    app.state.store = hub.store
    app.state.skill_registry = skill_registry
    app.state.config_reload = config_reload
    app.state.channels = channels
    app.state.session_origin = session_origin

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    @app.middleware("http")
    async def _broadcast_http_log(request: Request, call_next):
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
    ):
        app.include_router(r)

    # Read-only DB introspection. Opt-out via env (defaults on).
    if os.environ.get("YUYUTSAVA_DB_API_ENABLED", "true").lower() not in {"0", "false", "no"}:
        app.include_router(db_router.router)

    return app
