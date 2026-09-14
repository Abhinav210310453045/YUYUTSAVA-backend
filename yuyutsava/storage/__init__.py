"""Persistence: the backends, the store factory, and the core stores.

- Locations and formats: :mod:`~yuyutsava.storage.paths` (every persisted file),
  :mod:`~yuyutsava.storage.ids` (thread ids)
- Backends: :mod:`~yuyutsava.storage.backend` (SQLite by default, or Postgres),
  :mod:`~yuyutsava.storage.dialect` (the SQL seam that lets one store serve both),
  ``pg/`` (pool and migrations), ``routing/`` (SQLite buffers writes while Postgres
  is down), :mod:`~yuyutsava.storage.domains` (one declaration per table)
- Building stores: :class:`~yuyutsava.storage.factory.StoreFactory` resolves the
  backend once and builds the stores, including those that live in ``context``,
  ``memory``, ``skills``, ``visuals``, ``todoboard`` and ``daemon``;
  :class:`~yuyutsava.storage.context.AppContext` holds the handles;
  :mod:`~yuyutsava.storage.base` is the SQLite store base class and
  :mod:`~yuyutsava.storage.models` the typed records
- Stores kept here: ``sessions/``; ``events/`` (payloads, proposals, decisions,
  consent rules, quotas); ``interrupts``; ``prefs``; message feedback; voice
  messages. A ``*_unified`` module is the single implementation over both backends.
- Housekeeping: ``sweeper`` (TTL expiry), ``purge`` (delete everything for a
  session), ``introspect`` (read-only SQL for the ``/db`` API)

This package re-exports nothing: import a name from the module that owns it.
"""
