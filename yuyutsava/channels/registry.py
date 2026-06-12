"""Channel plugin lifecycle registry.

Modeled on :class:`yuyutsava.events.registry.SourceRegistry` (the
hot-reloadable, config-driven supervisor the master plan designates as the
pattern to copy): a static name→factory map, ``start_all`` from config,
``enable``/``disable`` at runtime, coarse ``reload``.

Enable = build plugin from config params → ``await plugin.start(sink)`` →
``router.register(plugin)``. Disable = ``router.unregister`` → ``await
plugin.stop()``. Both are idempotent under an asyncio lock, which also
guarantees the "never two pollers for one bot token" invariant: one
registry, one running instance per plugin name.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from yuyutsava.channels.config import ChannelConfig, ChannelsConfig
from yuyutsava.channels.plugin import ChannelPlugin, InboundSink

logger = logging.getLogger("yuyutsava.channels.registry")

PluginFactory = Callable[[dict[str, Any]], ChannelPlugin]


def _builtin_factories() -> dict[str, PluginFactory]:
    """Static plugin map (v1). Local import keeps httpx off the cold path."""
    from yuyutsava.channels.telegram.channel import TelegramChannelPlugin

    return {"telegram": TelegramChannelPlugin.from_config}


class ChannelPluginRegistry:
    """Owns running plugin instances; wires them into the ChannelRouter."""

    def __init__(
        self,
        *,
        router: object,                 # yuyutsava.daemon.channels.ChannelRouter
        sink: InboundSink,
        config: ChannelsConfig,
        plugin_factories: dict[str, PluginFactory] | None = None,
    ) -> None:
        self._router = router
        self._sink = sink
        self._config = config
        self._factories = (
            dict(plugin_factories) if plugin_factories is not None
            else _builtin_factories()
        )
        self._plugins: dict[str, ChannelPlugin] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start_all(self) -> None:
        """Enable every channel marked enabled in config. Boot must not die
        on one bad plugin (e.g. missing bot token) — log and continue."""
        for name, ch_cfg in self._config.channels.items():
            if not ch_cfg.enabled:
                logger.info("channel %s disabled in config; skipping", name)
                continue
            try:
                await self.enable(name)
            except Exception:
                logger.exception("channel %s failed to start; continuing boot", name)

    async def enable(self, name: str, params: dict[str, Any] | None = None) -> bool:
        """Start + register ``name``. Returns True when newly enabled,
        False when already running (double-enable is a no-op).

        Raises ``KeyError`` for an unknown plugin and propagates factory /
        ``start`` errors (missing token, bad config) to the caller.
        """
        async with self._lock:
            if name in self._plugins:
                return False
            factory = self._factories.get(name)
            if factory is None:
                raise KeyError(f"unknown channel plugin {name!r}")
            if params is None:
                cfg = self._config.channels.get(name)
                params = dict(cfg.params) if cfg is not None else {}
            plugin = factory(params)
            await plugin.start(self._sink)
            if not self._router.register(plugin):
                # Name collision with a non-plugin channel — refuse rather
                # than run an orphan poller the router never fans out to.
                await plugin.stop()
                raise RuntimeError(
                    f"channel name {plugin.name!r} already registered on the router"
                )
            self._plugins[name] = plugin
            logger.info("channel %s enabled (capabilities: %s)",
                        name, ", ".join(sorted(plugin.capabilities)) or "-")
            return True

    async def disable(self, name: str) -> bool:
        """Unregister + stop ``name``. Returns False when not running."""
        async with self._lock:
            plugin = self._plugins.pop(name, None)
            if plugin is None:
                return False
            self._router.unregister(plugin.name)
            try:
                await plugin.stop()
            except Exception:
                logger.exception("channel %s stop() failed", name)
            logger.info("channel %s disabled", name)
            return True

    async def stop_all(self) -> None:
        for name in list(self._plugins.keys()):
            await self.disable(name)

    async def reload(self, new_config: ChannelsConfig) -> None:
        """Hot-swap to *new_config* — coarse stop-then-start, matching
        SourceRegistry.reload's "I changed the config, restart them" model."""
        await self.stop_all()
        self._config = new_config
        await self.start_all()

    # ------------------------------------------------------------------ #
    # Introspection (GET /channels)                                       #
    # ------------------------------------------------------------------ #

    @property
    def config(self) -> ChannelsConfig:
        return self._config

    def set_config(self, config: ChannelsConfig) -> None:
        """Update the in-memory config (the HTTP layer persists the file)."""
        self._config = config

    def is_running(self, name: str) -> bool:
        return name in self._plugins

    def snapshot(self) -> list[dict[str, Any]]:
        """One row per known plugin: config state + live state + caps."""
        rows: list[dict[str, Any]] = []
        names = sorted(set(self._factories) | set(self._config.channels))
        for name in names:
            cfg = self._config.channels.get(name, ChannelConfig(name=name))
            plugin = self._plugins.get(name)
            caps: list[str] = sorted(plugin.capabilities) if plugin else []
            rows.append({
                "name": name,
                "available": name in self._factories,
                "enabled": cfg.enabled,
                "running": plugin is not None,
                "capabilities": caps,
            })
        return rows
