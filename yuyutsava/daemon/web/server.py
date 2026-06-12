"""Compatibility shim — exports moved to a modular package layout.

Historical imports such as ``from yuyutsava.daemon.web.server import
make_app, WebHub, WebChannel`` continue to work and resolve to the new
``app.create_app`` and ``services.stream_service`` symbols.
"""

from __future__ import annotations

from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.services.stream_service import WebChannel, WebHub


def make_app(
    hub,
    *,
    host,
    skill_registry=None,
    config_reload=None,
    channels=None,
    session_origin=None,
    auth=None,
    task_registry=None,
    task_submission=None,
    decision_service=None,
    channel_plugins=None,
    usage_store=None,
):
    """Backwards-compatible alias of :func:`create_app`.

    Extended to forward ``channels`` and ``session_origin`` (CLI Mode 2
    attach/detach router needs them), the Phase-2 gateway singletons
    (``auth``, ``task_registry``, ``task_submission``), and the Phase-3
    channel-plugin singletons (``decision_service``, ``channel_plugins``),
    and the Phase-4 ``usage_store``.
    """
    return create_app(
        hub,
        host=host,
        skill_registry=skill_registry,
        config_reload=config_reload,
        channels=channels,
        session_origin=session_origin,
        auth=auth,
        task_registry=task_registry,
        task_submission=task_submission,
        decision_service=decision_service,
        channel_plugins=channel_plugins,
        usage_store=usage_store,
    )


__all__ = ["WebHub", "WebChannel", "make_app", "create_app"]
