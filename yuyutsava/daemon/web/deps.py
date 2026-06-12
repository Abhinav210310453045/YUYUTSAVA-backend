"""FastAPI Dependencies for accessing daemon singletons attached at startup.

``create_app(...)`` stashes the long-lived objects (store, hub, skill registry,
event-registry-reload-event) on ``app.state``. Routers ask for what they need
via ``Depends(get_*)`` so they remain easy to test in isolation.
"""

from __future__ import annotations

from fastapi import Request

from yuyutsava.daemon.web.exceptions import ServiceUnavailableError


def get_hub(request: Request):
    hub = getattr(request.app.state, "hub", None)
    if hub is None:
        raise ServiceUnavailableError("web hub not initialized")
    return hub


def get_store(request: Request):
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise ServiceUnavailableError("store not initialized")
    return store


def get_skill_registry(request: Request):
    return getattr(request.app.state, "skill_registry", None)


def get_config_reload(request: Request):
    """Async callback that re-reads EventsConfig and rebinds fs watchers."""
    return getattr(request.app.state, "config_reload", None)


def get_channels(request: Request):
    """The daemon's ``ChannelRouter``. ``None`` only in tests that omit it."""
    return getattr(request.app.state, "channels", None)


def get_session_origin(request: Request):
    """The daemon's ``SessionOriginMap``. ``None`` when async subagents disabled."""
    return getattr(request.app.state, "session_origin", None)


def get_task_registry(request: Request):
    registry = getattr(request.app.state, "task_registry", None)
    if registry is None:
        raise ServiceUnavailableError("task registry not initialized")
    return registry


def get_task_submission(request: Request):
    submission = getattr(request.app.state, "task_submission", None)
    if submission is None:
        raise ServiceUnavailableError("task submission service not initialized")
    return submission


def get_decision_service(request: Request):
    """Shared proposal/ask resolver (also backs the channel InboundSink)."""
    service = getattr(request.app.state, "decision_service", None)
    if service is None:
        raise ServiceUnavailableError("decision service not initialized")
    return service


def get_usage_store(request: Request):
    """The ``llm_usage`` store (Phase 4 cost tracking)."""
    store = getattr(request.app.state, "usage_store", None)
    if store is None:
        raise ServiceUnavailableError("usage store not initialized")
    return store


def get_channel_plugins(request: Request):
    """The daemon's ``ChannelPluginRegistry`` (enable/disable at runtime)."""
    registry = getattr(request.app.state, "channel_plugins", None)
    if registry is None:
        raise ServiceUnavailableError("channel plugin registry not initialized")
    return registry
