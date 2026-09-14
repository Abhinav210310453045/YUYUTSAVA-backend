"""Daemon web logic shared beyond a single route.

- :mod:`~yuyutsava.daemon.web.services.decision_service` — ``DecisionService``, the
  single implementation of answering a proposal or an ask
- :mod:`~yuyutsava.daemon.web.services.stream_service` — ``WebHub``, the SSE
  broadcast hub, and ``WebChannel``, the web user channel
- :mod:`~yuyutsava.daemon.web.services.config_service` — ``ConfigService``, which
  reads, validates and persists the daemon's on-disk configs
- :mod:`~yuyutsava.daemon.web.services.config_schema` — the catalog of
  configuration variables served to the Settings UI

This package re-exports nothing: import a name from the module that owns it.
"""
