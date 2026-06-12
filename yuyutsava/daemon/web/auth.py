"""Bearer-token auth for the daemon API (off-loopback binds only).

Threat model, per the master plan: Tailscale ACLs are the outer wall — this
token is defense-in-depth inside the tailnet. Loopback binds stay
unauthenticated so the Electron renderer is untouched.

Rules:

- Loopback bind (``127.*`` / ``localhost`` / ``::1``): auth is NOT enforced.
- Non-loopback bind: every request needs ``Authorization: Bearer <token>``,
  compared constant-time. ``GET /health`` stays open as the reachability
  probe. ``?token=`` is accepted ONLY on ``/stream`` because EventSource
  cannot set headers (acceptable in-tailnet; never logged — the HTTP log
  middleware records the path without the query string).
- Token source: ``YUYUTSAVA_API_TOKEN`` env; when unset on a non-loopback
  bind, one is generated once into ``~/.yuyutsava/api_token`` (chmod 0600)
  and logged so the user can copy it to their phone.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from yuyutsava.storage.paths import state_dir

logger = logging.getLogger("yuyutsava.daemon.web.auth")

# Open even when auth is enforced: unauthenticated reachability probe
# (mobile app pings it to validate the server URL before asking for a token).
# Both the canonical /v1 path and the legacy alias are public.
_PUBLIC_PATHS = frozenset({"/health", "/v1/health"})

# The only paths where a query-string token is accepted (EventSource
# cannot set headers). /v1 canonical + legacy alias.
_QUERY_TOKEN_PATHS = frozenset({"/stream", "/v1/stream"})


def is_loopback_host(host: str) -> bool:
    """The bind-address test the app factory and AuthSettings share."""
    return host.startswith("127.") or host in ("localhost", "::1")


def _load_or_generate_token() -> str:
    """Read ``~/.yuyutsava/api_token``; generate + persist (0600) if absent."""
    path = state_dir() / "api_token"
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    logger.warning(
        "auth: no YUYUTSAVA_API_TOKEN set for a non-loopback bind — generated "
        "one at %s. Clients must send 'Authorization: Bearer <token>'.", path,
    )
    return token


@dataclass(frozen=True)
class AuthSettings:
    """Resolved auth posture for one app instance."""

    token: str = ""
    enforce: bool = False   # True ⇔ bind host is non-loopback

    @classmethod
    def from_env(cls, *, host: str) -> "AuthSettings":
        enforce = not is_loopback_host(host)
        token = os.environ.get("YUYUTSAVA_API_TOKEN", "").strip()
        if enforce and not token:
            token = _load_or_generate_token()
        return cls(token=token, enforce=enforce)


def check_request(
    settings: AuthSettings,
    *,
    path: str,
    authorization: str = "",
    query_token: str = "",
) -> bool:
    """Pure decision function the middleware (and tests) call.

    Returns True when the request may proceed.
    """
    if not settings.enforce or path in _PUBLIC_PATHS:
        return True
    supplied = ""
    if authorization.lower().startswith("bearer "):
        supplied = authorization[len("bearer "):].strip()
    if not supplied and path in _QUERY_TOKEN_PATHS:
        supplied = query_token
    if not supplied or not settings.token:
        return False
    return secrets.compare_digest(supplied, settings.token)


def install_auth_middleware(app: FastAPI, settings: AuthSettings) -> None:
    """Register the bearer-check middleware on *app*.

    Must be installed BEFORE CORSMiddleware is added (Starlette wraps
    later-added middleware outside earlier ones), so CORS preflights —
    which carry no Authorization header — are answered by the CORS layer
    and never reach the 401.
    """

    @app.middleware("http")
    async def _bearer_auth(request: Request, call_next):
        ok = check_request(
            settings,
            path=request.url.path,
            authorization=request.headers.get("authorization", ""),
            query_token=request.query_params.get("token", ""),
        )
        if not ok:
            return JSONResponse(
                status_code=401,
                content={
                    "code": "unauthorized",
                    "message": "missing or invalid bearer token",
                    "details": {},
                },
            )
        return await call_next(request)
