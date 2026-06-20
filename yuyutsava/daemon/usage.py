"""Per-LLM-call usage accounting: the ``llm_usage`` table + recorder middleware.

Every model call made by the orchestrator master or a subagent writes one
row — tokens in/out (from ``usage_metadata``, the ``BudgetMiddleware.
_accumulate`` pattern extended to output tokens) plus an estimated USD cost
from the :mod:`yuyutsava.core.model_router` price table. The join
``llm_usage × tasks`` is the audit surface for triage complexity noise
("complexity-1 tasks that burned 50k tokens") and feeds ``GET /usage``.

Same two-backend shape as :mod:`yuyutsava.daemon.task_registry`:

- :class:`SqliteUsageStore` — ``llm_usage`` table in ``state.db`` (own meta
  table; coexists with the events store via WAL).
- :class:`PgUsageStore` — the ``llm_usage`` table created by
  :mod:`yuyutsava.storage.pg.migrations` (v3).
"""

from __future__ import annotations

import dataclasses
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from ulid import ULID

from yuyutsava.core.model_router import estimate_cost_usd, load_price_table
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.daemon.usage")

GroupBy = Literal["task", "model", "day"]
_GROUP_BYS = ("task", "model", "day")


@dataclass(frozen=True)
class UsageContext:
    """Per-task identifiers stamped onto every usage row of one run.

    The orchestrator loop mints these per task (fresh graph per task makes
    this free) and hands them to ``build_orchestrator``.
    """

    task_id: str = ""
    thread_id: str = ""


@dataclass(frozen=True)
class UsageRow:
    """One LLM call, as persisted."""

    id: str                 # "usg_" + ULID
    ts: float               # epoch seconds
    thread_id: str
    task_id: str
    role: str               # "orchestrator" | subagent name | "cli" …
    model: str
    input_tokens: int
    output_tokens: int
    est_cost_usd: float

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class UsageAggregate:
    """One ``GET /usage`` result row (grouped or the single totals row)."""

    key: str                # task_id | model | YYYY-MM-DD | "all"
    calls: int
    input_tokens: int
    output_tokens: int
    est_cost_usd: float


def mint_usage_id() -> str:
    return f"usg_{ULID()}"


class UsageStore(ABC):
    """Persistence interface both backends implement."""

    @abstractmethod
    async def add(self, row: UsageRow) -> None: ...

    @abstractmethod
    async def list(
        self, *, task_id: str | None = None, since: float | None = None,
        limit: int = 200,
    ) -> list[UsageRow]:
        """Raw rows newest-first (audit / tests)."""

    @abstractmethod
    async def aggregate(
        self, *, since: float | None = None, group_by: GroupBy | None = None,
    ) -> list[UsageAggregate]:
        """Sums per group (or one ``key="all"`` row when ungrouped),
        most expensive group first."""


_SELECT_COLS = (
    "id, ts, thread_id, task_id, role, model, "
    "input_tokens, output_tokens, est_cost_usd"
)


def _row_to_record(row: Any) -> UsageRow:
    vals = tuple(row)
    # thread_id/task_id are NULL on Postgres for thread-less / task-less rows
    # (v4 normalized the old '' sentinels). Coerce back to '' so both backends
    # present the same wire contract that callers/tests have always seen.
    return UsageRow(
        id=vals[0], ts=vals[1], thread_id=vals[2] or "", task_id=vals[3] or "",
        role=vals[4], model=vals[5], input_tokens=vals[6],
        output_tokens=vals[7], est_cost_usd=vals[8],
    )


def _check_group_by(group_by: str | None) -> None:
    if group_by is not None and group_by not in _GROUP_BYS:
        raise ValueError(f"unknown group_by {group_by!r}; use one of {_GROUP_BYS}")


def _agg_rows(rows: list[Any]) -> list[UsageAggregate]:
    return [
        UsageAggregate(
            key=str(r[0]), calls=int(r[1]), input_tokens=int(r[2] or 0),
            output_tokens=int(r[3] or 0), est_cost_usd=float(r[4] or 0.0),
        )
        for r in rows
    ]


class SqliteUsageStore(BaseSqliteStore, UsageStore):
    """``llm_usage`` table inside ``state.db`` (zero-config fallback)."""

    _SCHEMA_VERSION = 1
    _META_TABLE = "llm_usage_meta"
    _SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS llm_usage_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS llm_usage (
            id            TEXT PRIMARY KEY,
            ts            REAL NOT NULL,
            thread_id     TEXT NOT NULL DEFAULT '',
            task_id       TEXT NOT NULL DEFAULT '',
            role          TEXT NOT NULL,
            model         TEXT NOT NULL,
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            est_cost_usd  REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS llm_usage_ts_idx ON llm_usage (ts);
        CREATE INDEX IF NOT EXISTS llm_usage_task_idx ON llm_usage (task_id);
    """

    # SQLite stores ts as epoch-seconds REAL; 'unixepoch' converts in place.
    _DAY_EXPR = "strftime('%Y-%m-%d', ts, 'unixepoch')"

    async def add(self, row: UsageRow) -> None:
        async def _do(conn):
            await conn.execute(
                "INSERT INTO llm_usage (id, ts, thread_id, task_id, role, "
                "model, input_tokens, output_tokens, est_cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row.id, row.ts, row.thread_id, row.task_id, row.role,
                 row.model, row.input_tokens, row.output_tokens,
                 row.est_cost_usd),
            )

        await self._run_write(_do)

    async def list(
        self, *, task_id: str | None = None, since: float | None = None,
        limit: int = 200,
    ) -> list[UsageRow]:
        await self._ensure_schema()
        where, args = _list_filters(task_id, since, "?")
        async with self._conn() as conn:
            cur = await conn.execute(
                f"SELECT {_SELECT_COLS} FROM llm_usage {where} "
                "ORDER BY id DESC LIMIT ?",
                (*args, limit),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_record(r) for r in rows]

    async def aggregate(
        self, *, since: float | None = None, group_by: GroupBy | None = None,
    ) -> list[UsageAggregate]:
        _check_group_by(group_by)
        await self._ensure_schema()
        sql, args = _aggregate_sql(group_by, since, self._DAY_EXPR, "?")
        async with self._conn() as conn:
            cur = await conn.execute(sql, args)
            rows = await cur.fetchall()
            await cur.close()
        return _agg_rows(rows)


class PgUsageStore(UsageStore):
    """``llm_usage`` table in Postgres (schema owned by pg/migrations.py v3)."""

    _DAY_EXPR = "to_char(to_timestamp(ts), 'YYYY-MM-DD')"

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def add(self, row: UsageRow) -> None:
        # '' -> NULL so the nullable FKs (llm_usage_thread_fk / llm_usage_task_fk)
        # are satisfied for thread-less / task-less rows (e.g. raw CLI calls).
        thread_id = row.thread_id or None
        task_id = row.task_id or None
        async with self._pool.connection() as conn:
            await ensure_thread(conn, thread_id)  # parent must exist for the FK
            await conn.execute(
                "INSERT INTO llm_usage (id, ts, thread_id, task_id, role, "
                "model, input_tokens, output_tokens, est_cost_usd) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (row.id, row.ts, thread_id, task_id, row.role,
                 row.model, row.input_tokens, row.output_tokens,
                 row.est_cost_usd),
            )

    async def list(
        self, *, task_id: str | None = None, since: float | None = None,
        limit: int = 200,
    ) -> list[UsageRow]:
        where, args = _list_filters(task_id, since, "%s")
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_SELECT_COLS} FROM llm_usage {where} "
                "ORDER BY id DESC LIMIT %s",
                (*args, limit),
            )
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def aggregate(
        self, *, since: float | None = None, group_by: GroupBy | None = None,
    ) -> list[UsageAggregate]:
        _check_group_by(group_by)
        sql, args = _aggregate_sql(group_by, since, self._DAY_EXPR, "%s")
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, args)
            rows = await cur.fetchall()
        return _agg_rows(rows)


def _list_filters(
    task_id: str | None, since: float | None, ph: str,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    args: list[Any] = []
    if task_id:
        where.append(f"task_id = {ph}")
        args.append(task_id)
    if since is not None:
        where.append(f"ts >= {ph}")
        args.append(since)
    return (f"WHERE {' AND '.join(where)}" if where else ""), args


def _aggregate_sql(
    group_by: GroupBy | None, since: float | None, day_expr: str, ph: str,
) -> tuple[str, tuple[Any, ...]]:
    """One GROUP BY statement shared by both backends (placeholder differs)."""
    key_expr = {
        # COALESCE keeps task-less rows (NULL task_id on Postgres) grouping
        # under '' exactly as the SQLite twin (NOT NULL DEFAULT '') always has.
        "task": "COALESCE(task_id, '')", "model": "model",
        "day": day_expr, None: "'all'",
    }[group_by]
    where = f"WHERE ts >= {ph}" if since is not None else ""
    args: tuple[Any, ...] = (since,) if since is not None else ()
    return (
        f"SELECT {key_expr} AS grp, COUNT(*), SUM(input_tokens), "
        f"SUM(output_tokens), SUM(est_cost_usd) "
        f"FROM llm_usage {where} GROUP BY grp "
        f"ORDER BY SUM(est_cost_usd) DESC, grp",
        args,
    )


# ---------------------------------------------------------------------------
# Recorder middleware
# ---------------------------------------------------------------------------


class UsageRecorder(AgentMiddleware):
    """Write one ``llm_usage`` row per model call (``aafter_model``).

    Constructed per agent build with the routed model's name and the task's
    join keys — the orchestrator builds a fresh graph per task, so these
    are fixed for the recorder's lifetime. Calls without ``usage_metadata``
    (fakes, providers that omit it) are skipped; a failed write is logged
    and swallowed — accounting must never break a run.
    """

    def __init__(
        self,
        store: UsageStore,
        *,
        role: str = "agent",
        model_name: str = "",
        task_id: str = "",
        thread_id: str = "",
        prices: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self._store = store
        self._role = role
        self._model_name = model_name
        self._task_id = task_id
        self._thread_id = thread_id
        self._prices = prices if prices is not None else load_price_table()

    @staticmethod
    def _tokens(usage: Any, field: str) -> int:
        n = usage.get(field) if isinstance(usage, dict) else getattr(usage, field, 0)
        return int(n or 0)

    async def aafter_model(self, state: Any, runtime: Any) -> None:
        messages = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        msg = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        usage = getattr(msg, "usage_metadata", None) if msg is not None else None
        if not usage:
            return None
        input_tokens = self._tokens(usage, "input_tokens")
        output_tokens = self._tokens(usage, "output_tokens")
        if not (input_tokens or output_tokens):
            return None
        model = self._model_name or str(
            (getattr(msg, "response_metadata", None) or {}).get("model_name", "")
        )
        row = UsageRow(
            id=mint_usage_id(),
            ts=time.time(),
            thread_id=self._thread_id,
            task_id=self._task_id,
            role=self._role,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_cost_usd=estimate_cost_usd(
                model, input_tokens, output_tokens, self._prices
            ),
        )
        try:
            await self._store.add(row)
        except Exception:
            logger.exception("llm_usage write failed (non-fatal)")
        return None
