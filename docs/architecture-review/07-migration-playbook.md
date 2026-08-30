# 07 — Store Migration Playbook

The repeatable procedure for collapsing one SQLite/Postgres twin pair onto the
dialect adapter. Derived from three completed migrations (visuals, thread
summaries, voice messages), each of which surfaced something the previous one
had not.

**Status:** 3 of 19 pairs done. Live count and per-domain notes in
[PROGRESS.md](PROGRESS.md).

---

## The procedure

### 1. Size it and read both twins end to end

```bash
.venv/bin/python -c "
import inspect, sys; sys.path.insert(0,'test/storage'); sys.path.insert(0,'.')
from test_twin_conformance import _all_twins
for l,_i,sq,pg in _all_twins():
    print(f'{l:34} {len(inspect.getsource(sq).splitlines()):4d} + {len(inspect.getsource(pg).splitlines()):4d}')"
```

Read for **differences that are not dialect**, because those are the ones that
bite. Every migration so far found at least one:

| Domain | Non-dialect difference |
|--------|------------------------|
| visuals | Row owns an on-disk PNG — deletes must unlink |
| summaries | `MAX()+1` version allocation — **raced on Postgres** |
| voice | Row *references* a blob it does **not** own — must NOT unlink |
| usage | Postgres nulls orphan `task_id`, SQLite keeps it — **user-visible** |
| transcripts / feedback / memory / artifacts | `created_ts` written by the **app** on SQLite and by the **database** on Postgres (findings AE, AH, AI, AJ) — check any timestamp column for this *before* migrating |
| tasks / interrupts | Postgres stores epoch seconds as **`DOUBLE PRECISION`**, not `TIMESTAMPTZ`. `Dialect.ts_param()`/`epoch()` are for `TIMESTAMPTZ` **only** — these bind with a plain `ph()` (finding AK) |

Note the visuals/voice pair: both rows carry a file path, and the correct
handling is opposite. Copying the previous migration without reading would have
double-deleted audio.

### 2. Write the parity suite FIRST

One `_Contract` mixin, four TestCases: SqliteTwin, SqliteUnified, PostgresTwin,
PostgresUnified. Copy the shape from
`test/storage/test_voice_store_parity.py`.

**Write a test for every claim you are about to make in a docstring.** The
summary-store race was found exactly this way — a test written to confirm an
assumption disproved it instead.

Postgres cases must skip cleanly when no server is reachable, so the suite runs
anywhere. With the daemon's Postgres up (`127.0.0.1:5433`), all four run.

### 3. Write the unified store

```python
class SomethingSchema(BaseSqliteStore):
    """SQLite DDL owner — no query methods.

    Copy _SCHEMA_VERSION / _META_TABLE / _SCHEMA_SQL VERBATIM from the twin so
    an existing state.db is picked up with no migration.
    """

class UnifiedSomethingStore(SomethingStore):
    def __init__(self, dialect: Dialect) -> None:
        self._d = dialect
```

Rules learned the hard way:

- **`d.write(fn)` for anything that mutates.** Never a bare connection: on
  Postgres the pool is autocommit, so a multi-statement write would commit
  partially.
- **`d.reading()` for queries.** Rows are mappings on both backends
  (`dict_row`), so write ONE row-mapper. If you find yourself writing two, the
  duplication has just moved rather than gone.
- **`d.epoch(col)`** in the select list for any timestamp — Postgres stores
  `timestamptz`, SQLite a REAL epoch.
- **`d.ts_param()`** when writing one.
- **`d.ensure_parent(conn, thread_id)`** before inserting into any table with
  the Postgres thread-hub FK.
- **`d.is_unique_violation(exc)`** if the domain allocates its own ids.

### 4. Run the parity suite; do not proceed until all four agree

If a twin fails and the unified store passes, you found a bug — say so
explicitly rather than quietly shipping the improvement.

### 5. Cut over, then delete

```bash
grep -rn "SqliteXStore\|PgXStore" yuyutsava --include="*.py" | grep -v __pycache__
```

Replace with the factories. **Then re-grep `test/` too** — three test files
still importing a deleted twin got missed on the summary migration and were
only caught by the full sweep.

Delete the twins and leave a NOTE comment in their place recording what replaced
them and which test justified it. Keep the shared vocabulary (record dataclass,
interface, validators, module-level constants) — only the two implementation
classes go.

### 6. Update the ratchets

- `test/storage/test_twin_conformance.py`: remove the pair, decrement the
  baseline, add a line to the history block.
- Parity suite: drop the twin TestCases, keep the unified ones (they become the
  cross-backend conformance for that domain).

### 7. Full sweep

```bash
.venv/bin/python scripts/verify_framework_contract.py
# every suite in test/storage, test/core, test/context, test/daemon
# plus a package-wide import walk
```

`test/web/test_converse_ws.py` and `test_voice_ws.py` fail with
`ModuleNotFoundError: pytest` — pre-existing and unrelated.

---

## Remaining domains, in recommended order

> **The whole `events:*` batch is done** (2026-08-08). Seven domains, zero
> hand-written twins left in the package. Twin pairs codebase-wide: **19 → 9**.
> Everything below is a store that owns its own `BaseSqliteStore`, so it uses
> `SqliteDialect` rather than `EventsSqliteDialect`.

| Order | Domain | Twin lines | Note |
|-------|--------|-----------:|------|
| — | **usage** | 124 | **PAUSED** — needs a product decision, see finding U |
| ~~1~~ | ~~events:ConsentRuleStore~~ | ~~44~~ | **DONE** |
| ~~2~~ | ~~events:ToolCounterStore~~ | ~~49~~ | **DONE** |
| ~~3~~ | ~~events:ConsentGrantStore~~ | ~~64~~ | **DONE** 2026-08-08 |
| ~~4~~ | ~~events:ProposalStore~~ | ~~64~~ | **DONE** 2026-08-08 — surfaced finding AC |
| ~~5~~ | ~~events:EventStore~~ | ~~92~~ | **DONE** 2026-08-08 — needed `Dialect.json_param/json_value` |
| ~~6~~ | ~~events:PendingAskStore~~ | ~~95~~ | **DONE** 2026-08-08 — wire helpers moved to `events/ask_wire.py` |
| ~~7~~ | ~~events:DecisionStore~~ | ~~101~~ | **DONE** 2026-08-08 |
| ~~8~~ | ~~transcript_store~~ | ~~158~~ | **DONE** 2026-08-08 — surfaced finding AE (two clocks) |
| ~~9~~ | ~~skills~~ | ~~156~~ | **DONE** 2026-08-08 — declared asymmetry established; finding AF |
| ~~10~~ | ~~task_registry~~ | ~~176~~ | **DONE** 2026-08-09 — finding AG |
| ~~11~~ | ~~feedback_store~~ | ~~190~~ | **DONE** 2026-08-09 — finding AH |
| ~~12~~ | ~~memory~~ | ~~190~~ | **DONE** 2026-08-09 — finding AI; **all 3 `getattr` probes deleted** |
| ~~13~~ | ~~artifacts~~ | ~~193~~ | **DONE** 2026-08-09 — finding AJ; `KNOWN_ASYMMETRIES` now empty |
| ~~14~~ | ~~interrupts~~ | ~~305~~ | **DONE** 2026-08-09 — findings AK, AL |
| ~~15~~ | ~~**todoboard**~~ | ~~**802**~~ | **DONE** 2026-08-09 — finding AM; contract validated against the twins *before* rewriting |

The seven `events:*` pairs share one `SqliteEventsBackend` and one `pg_stores.py`
module, so they are best migrated as a group rather than one at a time.

**Do not migrate a pgvector domain by copying a plain one.** `recall` and
`backfill_embeddings` are Postgres-only by nature and are recorded in
`KNOWN_ASYMMETRIES`; the unified store must keep them asymmetric, not invent a
SQLite stub.
