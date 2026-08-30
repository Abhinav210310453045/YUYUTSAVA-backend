"""Postgres twins for the events-domain stores (migration v9 tables).

Mirror the SQLite twins method-for-method. Differences from SQLite:

- ``%s`` placeholders; JSON columns are ``jsonb`` (insert with ``%s::jsonb``).
- psycopg returns ``jsonb`` already parsed to Python objects, so payload/value
  reads need no ``json.loads``. ``consent_rules.match_json`` is re-serialised on
  read because :class:`ConsentRule` keeps it as a JSON *string*.
- timestamps are ``double precision`` (epoch floats), identical to SQLite, so
  values are wire-identical and reconcile is a straight copy.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
from typing import Any

from ulid import ULID

from yuyutsava.storage.events.abc import (
    ConsentGrantStore,
    ConsentRuleStore,
    DecisionStore,
    EventStore,
    PendingAskStore,
    PrefsBackend,
    ProposalStore,
    ToolCounterStore,
)
# The pending_asks row<->record codecs are backend-agnostic (plain JSON text
# columns), so both twins share one implementation rather than drifting.
from yuyutsava.storage.events.sqlite_backend import (
    _ASK_COLS,
    ask_record_to_params,
    ask_row_to_record,
)
from yuyutsava.storage.models import ConsentRule, Decision, EventRecord, Proposal
from yuyutsava.storage.pg.pool import PgPool

logger = logging.getLogger("yuyutsava.storage.events.pg")



# NOTE: PgEventStore was replaced on 2026-08-08 by the Unified* store in
# events/unified.py (ADR-002 step 2.5b). Parity verified against both twins on
# both live backends in test/storage/test_events_unified_parity.py.


# NOTE: PgProposalStore was replaced on 2026-08-08 by the Unified* stores in events/unified.py
# (ADR-002 step 2.5b) — one implementation over the dialect adapter. Parity
# verified against both twins on both live backends in
# test/storage/test_events_unified_parity.py.



# NOTE: PgDecisionStore was replaced on 2026-08-08 by the Unified* stores in events/unified.py
# (ADR-002 step 2.5b) — one implementation over the dialect adapter. Parity
# verified against both twins on both live backends in
# test/storage/test_events_unified_parity.py.


class PgPrefsBackend(PrefsBackend):
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def put(self, key: str, value: Any) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                # updated_ts is TIMESTAMPTZ since migration v20.
                "INSERT INTO user_prefs(key, value_json, updated_ts) "
                "VALUES(%s,%s::jsonb,to_timestamp(%s)) "
                "ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json, "
                "updated_ts=EXCLUDED.updated_ts",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )

    async def delete(self, key: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM user_prefs WHERE key=%s", (key,))

    async def get(self, key: str, default: Any = None) -> Any:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT value_json FROM user_prefs WHERE key=%s", (key,))
            row = await cur.fetchone()
        if row is None:
            return default
        return row[0]

    async def list(self) -> dict[str, Any]:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT key, value_json FROM user_prefs ORDER BY key")
            rows = await cur.fetchall()
        return {r[0]: r[1] for r in rows}



# NOTE: PgConsentGrantStore was replaced on 2026-08-08 by the Unified* stores in events/unified.py
# (ADR-002 step 2.5b) — one implementation over the dialect adapter. Parity
# verified against both twins on both live backends in
# test/storage/test_events_unified_parity.py.



# NOTE: PgPendingAskStore was replaced on 2026-08-08 by UnifiedPendingAskStore
# in events/unified.py (ADR-002 step 2.5b). Parity verified against both twins on
# both live backends in test/storage/test_events_unified_parity.py.
