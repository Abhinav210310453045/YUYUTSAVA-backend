"""Configuration with no dependencies, which every layer may read.

    env        reading settings from the environment (``env``, ``env_flag``, …)
    limits     size limits and timing (``LIMITS``, ``TIMING``)
    execution  where tools run (``DockerSettings``, ``LocalSettings``)
    search     web search providers (``SearchConfig``)

This package imports nothing from ``yuyutsava`` outside itself. It re-exports
nothing: import a name from the module that owns it. Settings that belong to one
subsystem live with it — ``llm.settings``, ``events.config``, ``daemon.config``.
"""
