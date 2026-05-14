"""User preferences store backed by the existing ``state.db``.

Each preference is a small JSON blob keyed by a dot-namespaced string:
``interaction.style``, ``media.tone``, ``spotify.prefs``, etc.

Reads are synchronous (same thread that owns the SQLite connection).
Writes go through ``Store``'s async writer queue so they never block the loop.

CLI: ``yuyutsava prefs set <key> <json>``
         ``yuyutsava prefs get <key>``
         ``yuyutsava prefs list``
"""

from __future__ import annotations

import json
import logging
from typing import Any

from yuyutsava.events.store import Store

logger = logging.getLogger("yuyutsava.prefs.store")


class UserPrefsStore:
    """Thin API over the ``user_prefs`` table in ``state.db``."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------ #
    # Write (async)                                                         #
    # ------------------------------------------------------------------ #

    async def set(self, key: str, value: Any) -> None:
        """Upsert ``value`` (any JSON-serialisable object) at ``key``."""
        await self._store.put_pref(key, value)
        logger.debug("prefs: set %s", key)

    async def delete(self, key: str) -> None:
        """Remove a preference key. No-op if the key doesn't exist."""
        await self._store.delete_pref(key)
        logger.debug("prefs: deleted %s", key)

    # ------------------------------------------------------------------ #
    # Read (sync)                                                           #
    # ------------------------------------------------------------------ #

    def get(self, key: str, default: Any = None) -> Any:
        """Return the stored value for ``key``, or ``default`` if absent."""
        return self._store.get_pref(key, default)

    def all(self) -> dict[str, Any]:
        """Return all stored preferences as a ``{key: value}`` dict."""
        return self._store.list_prefs()
