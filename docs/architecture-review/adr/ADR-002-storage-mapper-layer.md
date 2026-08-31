# ADR-002 — Collapse the store twins behind a dialect adapter and a domain registry

**Status:** Proposed
**Addresses:** [`F-D02`](../03-findings-dry-kiss.md#f-d02), [`F-D04`](../03-findings-dry-kiss.md#f-d04), [`F-S04`](../02-findings-solid.md#f-s04), [`F-S07`](../02-findings-solid.md#f-s07), [`F-S10`](../02-findings-solid.md#f-s10), [`F-S12`](../02-findings-solid.md#f-s12)
**Phase:** 2

---

## Context

17 persisted domains are each implemented three times — an ABC, a SQLite class,
and a Postgres class — for **51 hand-maintained store classes**. The same
business rules are written twice in two SQL dialects.

Consequences already realized:

- **Divergent semantics.** SQLite twins run inside `BEGIN IMMEDIATE` with
  retry-on-busy via `BaseSqliteStore._run_write`; Postgres twins have neither
  ([`F-S10`](../02-findings-solid.md#f-s10)). Dev and prod do not agree.
- **Two schema authorities.** DDL lives in 16 modules *and* in an 866-line
  `storage/pg/migrations.py`, with two unrelated version counters
  ([`F-D04`](../03-findings-dry-kiss.md#f-d04)).
- **Hardcoded lifecycle lists.** `storage/purge.py:79` `_PG_CHILD_TABLES` is an
  11-entry tuple with a SQLite twin. Forgetting an entry means session deletion
  silently orphans rows — a data-retention bug with no test to catch it.
- **13 backend branches** in `build_daemon`, plus a *second*, contradictory
  policy (`RoutedStore`) applied to 3 of 17 stores with no documentation of why
  ([`F-S07`](../02-findings-solid.md#f-s07)).

## Decision

**One implementation per domain, over a thin dialect adapter, with lifecycle
behavior derived from a domain registry.**

### 1. Dialect adapter

```python
class Dialect(Protocol):
    def placeholder(self, i: int) -> str:              # "?" | "%s"
    def now_expr(self, param: str) -> str:             # "?" | "to_timestamp(%s)"
    def upsert(self, table, cols, conflict_col) -> str
    def returning(self, cols: Sequence[str]) -> str    # "" | "RETURNING …"
    async def transaction(self, conn) -> AsyncContextManager
```

`transaction()` is the piece that dissolves `F-S10`: one transaction policy,
two implementations, applied by the shared base rather than rediscovered per twin.

### 2. One store implementation per domain

```python
class TodoStoreImpl(DomainStore[TodoCardV1]):
    async def assign_note(self, note_id, objective_id, phase, updated_ts):
        async with self.tx() as conn:
            row = await self.exec_one(conn,
                "UPDATE todo_notes SET objective_id = {0}, phase = {1}, "
                "updated_ts = {now2} WHERE note_id = {3}",
                objective_id, phase, updated_ts, note_id, returning=("card_id",))
            if row is None:
                return None
            await self.exec(conn,
                "UPDATE todo_cards SET updated_ts = {now0} WHERE card_id = {1}",
                updated_ts, row["card_id"])
            return await self.get_note(conn, note_id)
```

The rule — *"reassign, touch the parent card, return the note"* — is written
**once**. Compare the two current copies at `todoboard/store.py:470` and `:904`.

### 3. Domain registry

```python
@domain(table="todo_cards", thread_scoped=True, retention_days=None,
        failover=Failover.SPILLOVER)
@dataclass(frozen=True)
class TodoCardV1: ...
```

`purge.py`'s two hardcoded lists become **derived** from `thread_scoped`; the
sweeper's per-domain methods become derived from `retention_days`; `RoutedStore`
application becomes derived from `failover` — so "which stores survive a Postgres
outage" is finally declared rather than accidental.

## Alternatives considered

### A. Adopt an ORM (SQLAlchemy / SQLModel)

**Rejected**, though it is the obvious candidate and solves the dialect problem
outright.

- The codebase is fully async with two hand-tuned drivers (`aiosqlite`,
  `psycopg`) and a custom pool with loop-affinity requirements — a known,
  non-negotiable constraint of this system.
- pgvector search (`retrieval/pg.py`) is hand-written for a reason and would need
  raw SQL escape hatches anyway.
- Migrating 17 domains to an ORM is strictly larger than migrating them to a
  thin adapter, and it imports a large framework dependency into the one layer
  currently free of framework coupling — directly contrary to
  [ADR-004](ADR-004-framework-boundary.md).

**Reconsider if** the domain count roughly doubles, or if a third backend is
required.

### B. Query builder (e.g. `pypika`) instead of format-string SQL

**Partially adopted.** A builder for the mechanical CRUD paths is good; forcing
*all* queries through one is not — the recursive todo queries and the pgvector
searches are clearer as raw SQL. The adapter above deliberately supports both:
builder for CRUD, `dialect.placeholder()` for hand-written SQL.

### C. Drop SQLite; require Postgres

**Rejected.** SQLite is the zero-config default and a genuine product property —
the CLI works with no database setup. Removing it trades a structural problem for
a user-experience regression.

### D. Code generation from a schema definition

**Rejected.** Generation moves the duplication into a build step and makes the
generated code un-editable when a domain needs something bespoke. The adapter
achieves the same collapse without a codegen pipeline in the loop.

### E. Do nothing

**Rejected.** This is the largest single cost in the codebase: ~8,000 duplicated
lines, ~200 duplicated method pairs, two documented silent data-loss traps, and a
divergence that has *already* occurred in transaction semantics.

## Consequences

### Positive

- 51 store classes → ~17 implementations + 2 dialect adapters.
- Scenario B (new domain): ~515 lines → ~120, and the purge/sweep traps become
  structurally impossible rather than merely documented. See
  [05](../05-change-cost-scenarios.md#scenario-b--add-a-new-persisted-domain).
- `F-S10` dissolves: one transaction policy by construction.
- `F-S04`'s 13 branches collapse to one `StoreFactory`.
- `F-S07`'s failover policy becomes declared data.
- One schema authority per domain.

### Negative

- **This is the data path.** A mistake here loses or corrupts user data. It is
  the highest-risk phase in the plan and is deliberately sequenced behind a full
  conformance suite.
- Format-string SQL with dialect placeholders is less readable than either raw
  dialect SQL or a typed builder. Mitigate with a small, well-documented
  substitution vocabulary (`{0}`, `{now2}`) and a lint rule against ad-hoc
  interpolation.
- 4 weeks, no user-visible benefit.
- Domains with genuinely backend-specific behavior (pgvector search) still need
  a per-backend path. **Keep those explicit** — a `PgVectorSearch` that only
  works on Postgres is honest; a fake abstraction over it is not.

### Risk and mitigation

| Risk | Mitigation |
|------|-----------|
| Data loss / corruption | Conformance suite (Step 2.1) written and green on both old implementations **before** any migration |
| Silent behavior change | Per-domain migration; each domain ships independently and reversibly |
| Hidden coupling in a domain | Migrate visuals first (small, on-disk side effects, already `RoutedStore`-wrapped) to surface assumptions cheaply |
| Conformance suite skipped for time | **Cut phases, not this step.** It is what makes the rest mechanical |
| PG tests need a live database | Marker-gated; SQLite half stays fast and always-on |

## Verification

- Per-domain conformance suite, parameterized over both backends, green before
  and after each migration
- Registry-completeness test: every `thread_scoped=True` domain is reachable by
  `purge_session`
- Schema-parity test: introspect both backends, assert identical logical columns
  per table (this alone would have caught every historical `F-D04` drift)
- Store class count ≤ 40; `CREATE TABLE` in ≤ 2 modules
