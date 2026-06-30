"""User preferences store backed by the shared ``state.db``.

Each preference is a small JSON blob keyed by a dot-namespaced string:
``interaction.style``, ``media.tone``, ``spotify.prefs``, etc.

Reads and writes are both ``async`` — the backing :class:`Store` may be
Postgres-backed, where a synchronous read would block the event loop.

This module is the typed wrapper that callers should depend on. The actual
``user_prefs`` table is owned by the :class:`PrefsBackend` twin behind the
:class:`Store` facade (:mod:`yuyutsava.storage.events`).

CLI:
    ``yuyutsava prefs set <key> <json>``
    ``yuyutsava prefs get <key>``
    ``yuyutsava prefs list``
"""

from __future__ import annotations

import logging
from typing import Any

from yuyutsava.storage.events.store import Store

logger = logging.getLogger("yuyutsava.storage.prefs")


class PrefsStore:
    """Typed API over the ``user_prefs`` table.

    Construct with the same :class:`Store` instance the daemon (or the CLI's
    ``prefs`` subcommand) already created — the connection is shared.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------ #
    # Write (async — queues through Store's writer task)                  #
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
    # Read (async — a Postgres-backed store must not block the loop)       #
    # ------------------------------------------------------------------ #

    async def get(self, key: str, default: Any = None) -> Any:
        """Return the stored value for ``key``, or ``default`` if absent."""
        return await self._store.get_pref(key, default)

    async def all(self) -> dict[str, Any]:
        """Return all stored preferences as a ``{key: value}`` dict."""
        return await self._store.list_prefs()
