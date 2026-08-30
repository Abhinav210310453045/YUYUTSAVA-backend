"""Per-LLM-call usage accounting: the ``llm_usage`` table + recorder middleware.

Every model call made by the orchestrator master or a subagent writes one
row — tokens in/out (from ``usage_metadata``, the ``BudgetPolicy.
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

from ulid import ULID

from yuyutsava.core.model_router import estimate_cost_usd, load_price_table
from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import Directive, Turn
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.pg.threads import ensure_thread

logger = logging.getLogger("yuyutsava.daemon.usage")

GroupBy = Literal["task", "model", "day", "thread"]
_GROUP_BYS = ("task", "model", "day", "thread")


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


#: SQLite read list — ``ts`` is already a REAL epoch.
_SELECT_COLS = (
    "id, ts, thread_id, task_id, role, model, "
    "input_tokens, output_tokens, est_cost_usd"
)

#: Postgres read list. ``ts`` became TIMESTAMPTZ in migration v20, so it is
#: projected back to an epoch float — ``UsageRow.ts`` is a float and every
#: caller does arithmetic on it. ``::float8`` matters: ``extract(epoch ...)``
#: alone yields ``numeric``, which psycopg returns as ``Decimal``.
_PG_SELECT_COLS = (
    "id, extract(epoch FROM ts)::float8 AS ts, thread_id, task_id, role, model, "
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

    # ts is TIMESTAMPTZ since migration v20 — no to_timestamp() needed.
    _DAY_EXPR = "to_char(ts, 'YYYY-MM-DD')"

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def add(self, row: UsageRow) -> None:
        # '' -> NULL so the nullable FKs (llm_usage_thread_fk / llm_usage_task_fk)
        # are satisfied for thread-less / task-less rows (e.g. raw CLI calls).
        thread_id = row.thread_id or None
        task_id = row.task_id or None
        # task_id may name a conversation that was never registered in `tasks`
        # (e.g. the tinker card recorder tags "tinker:<card_id>", which is a chat
        # thread, not an orchestrator task). Resolve it through a guard subquery:
        # an existing task keeps its id; an orphan (or NULL) collapses to NULL, so
        # llm_usage_task_fk is always satisfied — the same orphan-nulling the v4
        # migration applies to the backfill. Attribution for real tasks is
        # untouched (the orchestrator inserts the task row before its first call).
        async with self._pool.connection() as conn:
            await ensure_thread(conn, thread_id)  # parent must exist for the FK
            await conn.execute(
                "INSERT INTO llm_usage (id, ts, thread_id, task_id, role, "
                "model, input_tokens, output_tokens, est_cost_usd) "
                # ts is TIMESTAMPTZ since migration v20.
                "VALUES (%s, to_timestamp(%s), %s, "
                "(SELECT task_id FROM tasks WHERE task_id = %s), "
                "%s, %s, %s, %s, %s)",
                (row.id, row.ts, thread_id, task_id, row.role,
                 row.model, row.input_tokens, row.output_tokens,
                 row.est_cost_usd),
            )

    async def list(
        self, *, task_id: str | None = None, since: float | None = None,
        limit: int = 200,
    ) -> list[UsageRow]:
        where, args = _list_filters(task_id, since, "%s", "to_timestamp(%s)")
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_PG_SELECT_COLS} FROM llm_usage {where} "
                "ORDER BY id DESC LIMIT %s",
                (*args, limit),
            )
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def aggregate(
        self, *, since: float | None = None, group_by: GroupBy | None = None,
    ) -> list[UsageAggregate]:
        _check_group_by(group_by)
        sql, args = _aggregate_sql(group_by, since, self._DAY_EXPR, "%s", "to_timestamp(%s)")
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, args)
            rows = await cur.fetchall()
        return _agg_rows(rows)


def _list_filters(
    task_id: str | None, since: float | None, ph: str, ts_ph: str | None = None,
) -> tuple[str, list[Any]]:
    """``ts_ph`` is the placeholder for comparing against ``ts``.

    Separate from ``ph`` because ``llm_usage.ts`` is ``TIMESTAMPTZ`` on Postgres
    (migration v20) and a REAL epoch on SQLite, so the float bound here needs a
    ``to_timestamp()`` wrapper on one backend and nothing on the other.
    """
    ts_ph = ts_ph or ph
    where: list[str] = []
    args: list[Any] = []
    if task_id:
        where.append(f"task_id = {ph}")
        args.append(task_id)
    if since is not None:
        where.append(f"ts >= {ts_ph}")
        args.append(since)
    return (f"WHERE {' AND '.join(where)}" if where else ""), args


def _aggregate_sql(
    group_by: GroupBy | None, since: float | None, day_expr: str, ph: str,
    ts_ph: str | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """One GROUP BY statement shared by both backends (placeholders differ).

    ``ts_ph`` wraps the ``since`` bound — see :func:`_list_filters`.
    """
    ts_ph = ts_ph or ph
    key_expr = {
        # COALESCE keeps task-less rows (NULL task_id on Postgres) grouping
        # under '' exactly as the SQLite twin (NOT NULL DEFAULT '') always has.
        "task": "COALESCE(task_id, '')", "model": "model",
        "day": day_expr, None: "'all'",
        # Group by conversation rather than orchestrator task.
        #
        # ``task_id`` is FK-constrained to ``tasks`` on Postgres, so a tag that
        # does not name a real task is nulled on insert — the TinkerAgent's
        # ``tinker:<card_id>`` is exactly that case. The card identity is never
        # lost, though: it lives in ``thread_id`` (``todo:<card_id>``), which
        # has no such constraint and is written identically on both backends.
        #
        # So "what did card 42 cost me?" was always answerable; the reporting
        # API just could not reach the column holding the answer. Adding this
        # grouping is what makes per-conversation cost work on Postgres without
        # weakening ``llm_usage_task_fk`` or changing what ``task_id`` means.
        "thread": "COALESCE(thread_id, '')",
    }[group_by]
    where = f"WHERE ts >= {ts_ph}" if since is not None else ""
    args: tuple[Any, ...] = (since,) if since is not None else ()
    return (
        f"SELECT {key_expr} AS grp, COUNT(*), SUM(input_tokens), "
        f"SUM(output_tokens), SUM(est_cost_usd) "
        f"FROM llm_usage {where} GROUP BY grp "
        f"ORDER BY SUM(est_cost_usd) DESC, grp",
        args,
    )


# ---------------------------------------------------------------------------
# Recorder policy
# ---------------------------------------------------------------------------


class UsagePolicy(Policy):
    """Write one ``llm_usage`` row per model call.

    Phase 4 step 4.8, fourteenth and last migration (was ``UsageRecorder``).

    Constructed per agent build with the routed model's name and the task's join
    keys — the orchestrator builds a fresh graph per task, so these are fixed for
    the policy's lifetime.

    Two things it must never do: fail a run, and invent numbers. A failed write
    is logged and swallowed; a call the provider reported no usage for is
    skipped rather than recorded as zero, because a zero row is
    indistinguishable from a genuinely free call when the costs are summed.
    """

    name = "UsagePolicy"

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
        super().__init__()
        self._store = store
        self._role = role
        self._model_name = model_name
        self._task_id = task_id
        self._thread_id = thread_id
        self._prices = prices if prices is not None else load_price_table()

    async def after_model(self, turn: Turn) -> Directive | None:
        usage = turn.usage
        if usage is None or not usage.any_tokens:
            return None
        # The build-time name wins; `usage.model` is what the provider called it
        # on this particular response, and is the fallback.
        model = self._model_name or usage.model
        row = UsageRow(
            id=mint_usage_id(),
            ts=time.time(),
            thread_id=self._thread_id,
            task_id=self._task_id,
            role=self._role,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            est_cost_usd=estimate_cost_usd(
                model, usage.input_tokens, usage.output_tokens, self._prices
            ),
        )
        try:
            await self._store.add(row)
        except Exception:
            logger.exception("llm_usage write failed (non-fatal)")
        return None
