"""Read/validate/persist the on-disk daemon configs.

Currently focused on ``events_config.json``. Permissions and LLM (.env) writes
can be layered on later without touching routers.

After every write, ``reload_callback()`` is invoked so the daemon can hot-swap
the affected source (e.g. restart the fs watcher with new roots) without
requiring the user to restart the daemon.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from yuyutsava.core.config import EventsConfig
from yuyutsava.daemon.web.exceptions import ValidationError

logger = logging.getLogger("yuyutsava.daemon.web.config_service")

ReloadCallback = Callable[[], Awaitable[None]] | None


def _validate_root(path: str) -> Path:
    if not path or not isinstance(path, str):
        raise ValidationError("path must be a non-empty string")
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValidationError(f"path must be absolute (got {path!r})")
    if not p.exists():
        raise ValidationError(f"path does not exist: {p}")
    if not p.is_dir():
        raise ValidationError(f"path is not a directory: {p}")
    return p


def _validate_events_config(cfg: EventsConfig) -> None:
    fs = cfg.sources.get("fs")
    if fs is None:
        return
    coalesce = fs.params.get("coalesce_window_ms", 750)
    try:
        coalesce_int = int(coalesce)
    except (TypeError, ValueError):
        raise ValidationError("coalesce_window_ms must be an integer")
    if not (50 <= coalesce_int <= 60_000):
        raise ValidationError("coalesce_window_ms must be in [50, 60000]")
    for r in fs.params.get("roots") or []:
        _validate_root(str(r))


class ConfigService:
    """Read + write events_config.json, then trigger a hot reload."""

    def __init__(self, reload_callback: ReloadCallback = None) -> None:
        self._reload = reload_callback
        self._lock = asyncio.Lock()

    def get_events(self) -> EventsConfig:
        return EventsConfig.from_file()

    async def save_events(self, cfg: EventsConfig) -> EventsConfig:
        _validate_events_config(cfg)
        async with self._lock:
            cfg.to_file()
            if self._reload is not None:
                try:
                    await self._reload()
                except Exception:
                    logger.exception("config reload callback failed")
        return cfg

    async def add_root(self, path: str) -> list[str]:
        validated = _validate_root(path)
        cfg = self.get_events().with_fs_root_added(str(validated))
        await self.save_events(cfg)
        fs = cfg.sources.get("fs")
        return list((fs.params.get("roots") if fs else []) or [])

    async def remove_root(self, path: str) -> list[str]:
        cfg = self.get_events().with_fs_root_removed(path)
        await self.save_events(cfg)
        fs = cfg.sources.get("fs")
        return list((fs.params.get("roots") if fs else []) or [])
