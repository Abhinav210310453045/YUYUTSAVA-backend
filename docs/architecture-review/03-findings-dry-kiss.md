# 03 — DRY & KISS Findings

**DRY is about knowledge, not text.** A duplication finding here means *the same
decision is encoded in more than one place*, so changing the decision requires
finding every copy. Low textual similarity does not refute this — see the note
under `F-D02`.

**Index**

| ID | Kind | Title | Severity |
|----|------|-------|----------|
| [F-D01](#f-d01) | DRY | Three agent builders encode the same assembly sequence | P0 |
| [F-D02](#f-d02) | DRY | 17 domains × 2 backends: the same business rules written twice by hand | P0 |
| [F-D03](#f-d03) | DRY | Two parallel graph-driving loops, one per output style | P1 |
| [F-D04](#f-d04) | DRY | Two competing schema-ownership mechanisms | P1 |
| [F-D05](#f-d05) | DRY | Four hand-maintained lists of the same provider set | P2 |
| [F-D06](#f-d06) | DRY | Agent stack assembly duplicated across CLI, daemon, and tinker | P1 |
| [F-K01](#f-k01) | KISS | 12 functions over 200 lines; peak cyclomatic 116 | P1 |
| [F-K02](#f-k02) | KISS | Parameter lists of 32, 30, 25, and 25 | P1 |
| [F-K03](#f-k03) | KISS | Import placement is load-bearing behavior | P1 |
| [F-K04](#f-k04) | KISS | Dead and half-built structure left in place | P2 |

---

## F-D01

### Three agent builders encode the same assembly sequence

**Severity:** **P0**
**Location:** `core/engine.py:431`, `:759`, `:1003`

Full evidence and the drift matrix are in [`F-S02`](02-findings-solid.md#f-s02),
where this is analysed as the Open/Closed violation it primarily is. Recorded
here because it is also the largest DRY violation by *decision count*: nine
assembly steps and their ordering constraints are encoded three times.

**The duplicated knowledge**, specifically — each of these is a decision the
system has made exactly once conceptually, and written down three times:

1. Middleware order is `tool filter → offload → compaction → budget → usage`.
   The ordering rationale is re-explained in comments at `:543-545`, `:936-938`,
   `:1088-1089`.
2. Async subagents require a host URL; absence is a `ValueError` (`:646`,
   `:903`, `:1181` — three copies of the same guard and message).
3. Retrieval injectors are built by null-checking each store in turn, then
   wrapped in one `RetrievalInjectionMiddleware` (`:564-584`, `:960-978`,
   `:1108-1131`).
4. `ctx_*` tools must be freshly instantiated per subagent spec, never shared
   (`:423-426`, `:906-909`).
5. Async specs are identified by dict shape — `"graph_id" in s and "url" in s`
   (`:670`, `:915`, `:1193`).

Point 5 is worth isolating: **the identity test for an async subagent spec is a
duck-typed dict-key check, written three times.** If the spec shape ever gains a
field, three sites must agree.

**Direction:** [ADR-001](adr/ADR-001-agent-build-pipeline.md).

---

## F-D02

### 17 domains × 2 backends: the same business rules written twice by hand

**Severity:** **P0**
**Location:** the full triple table is in
[01 § M3](01-evidence-and-metrics.md#m3--storage-topology)

#### Claim

Every persisted domain is written three times — an ABC, a SQLite class, and a
Postgres class — producing 51 hand-maintained store classes in which the same
business rules are expressed twice in two SQL dialects.

#### Evidence

The canonical example. One rule — *"reassign a note; touch the parent card's
`updated_ts`; return the note"* — implemented twice, 434 lines apart in the same
file:

```python
# SqliteTodoStore.assign_note — todoboard/store.py:470
async def _do(conn):
    cur = await conn.execute(
        "UPDATE todo_notes SET objective_id = ?, phase = ?, updated_ts = ? WHERE note_id = ?",
        (objective_id, phase, updated_ts, note_id))
    if (cur.rowcount or 0) == 0:
        return None
    cur = await conn.execute("SELECT * FROM todo_notes WHERE note_id = ?", (note_id,))
    row = await cur.fetchone()
    if row is not None:
        await conn.execute("UPDATE todo_cards SET updated_ts = ? WHERE card_id = ?",
                           (updated_ts, row["card_id"]))
    return _sqlite_note(row) if row else None
return await self._run_write(_do)
```

```python
# PgTodoStore.assign_note — todoboard/store.py:904
async with self._pool.connection() as conn:
    cur = await conn.execute(
        "UPDATE todo_notes SET objective_id = %s, phase = %s, "
        "updated_ts = to_timestamp(%s) WHERE note_id = %s RETURNING card_id",
        (objective_id, phase, updated_ts, note_id))
    row = await cur.fetchone()
    if row is None:
        return None
    await conn.execute("UPDATE todo_cards SET updated_ts = to_timestamp(%s) WHERE card_id = %s",
                       (updated_ts, row[0]))
    cur = await conn.execute(f"SELECT {_PG_NOTE_COLS} FROM todo_notes WHERE note_id = %s", (note_id,))
    note_row = await cur.fetchone()
return _pg_note(note_row) if note_row else None
```

`TodoStore` has **21 such method pairs**. Across 17 domains the pattern repeats
at roughly 200 method pairs. `todoboard/store.py` alone is 1122 lines, of which
~930 are the two twins.

> **On the similarity numbers.** Textual diff puts these twins at 22–38%
> similar (M3). That figure is a *measurement of the dialect gap, not of the
> knowledge gap*. The method names match one-for-one, the order matches, the
> contracts match, the null-handling matches, the parent-touch rule matches. What
> differs is `?` vs `%s`, `row["card_id"]` vs `row[0]`, and `to_timestamp()`.
> Judge the duplication by the method-correspondence table in
> [01 § M3](01-evidence-and-metrics.md#m3--storage-topology), not by the diff
> ratio.

#### Consequence

**1 — Divergence is undetectable.** Fix a bug in `SqliteTodoStore.update_card`
and nothing tells you `PgTodoStore.update_card` has the same bug. There is no
shared test suite that both must pass; each is tested, if at all, against
itself. `F-S10` documents a divergence that has *already* happened, in
transaction semantics.

**2 — The cost is paid on every feature.** Adding one field to a todo card
touches: the SQLite `CREATE TABLE`, the SQLite `_migrate` step, the SQLite
row-mapper, ~4 SQLite methods, the Postgres migration in `storage/pg/migrations.py`,
the `_PG_*_COLS` constant, the Postgres row-mapper, ~4 Postgres methods, and the
model dataclass. **~12 edits for one field.**

**3 — It is the single largest source of code mass.** Roughly 8,000 lines across
the storage surface are the second copy.

**4 — It compounds every other finding.** It is why `build_daemon` has 13
backend branches (`F-S04`), why interface width matters (`F-S12`), and why
schema DDL is scattered (`F-D04`).

#### Why it happened, and why that matters for the fix

This is not carelessness — it is the honest, obvious way to support two backends
before a mapper exists. The structural tell is that the twins are *parallel*:
same names, same order, same contracts. Divergent duplication is hard to
collapse; **parallel duplication is exactly the shape a mapper layer collapses
mechanically.** That is what makes this the highest-leverage fix in the review
rather than a rewrite.

#### Direction

One implementation per domain over a thin dialect adapter — see
[ADR-002](adr/ADR-002-storage-mapper-layer.md). Target: 51 classes → ~17
implementations + 2 dialect adapters.

---

## F-D03

### Two parallel graph-driving loops, one per output style

**Severity:** **P1**
**Location:** `core/streaming.py:363` (`astream_agent_iter`), `:593` (`astream_agent`)

#### Claim

The logic for driving a compiled graph — streaming steps, detecting interrupts,
resuming with `Command(resume=…)`, handling multi-interrupt batches, tracking
token attribution — exists twice, differing only in where output goes.

#### Evidence

| | `astream_agent_iter` (:363) | `astream_agent` (:593) |
|---|---|---|
| Lines / branches | 228 / 54 | 226 / 58 |
| Output | `yield StreamEvent(...)` | prints to stderr, returns `str` |
| Consumer | daemon | CLI |
| Interrupt handling | `ask_handler` callback | stdin prompt |
| `Command(resume=…)` sites | `:575`, `:580`, `:590` | `:797`, `:804` |

Both implement the same non-obvious rule, independently:

> *"LangGraph requires `Command(resume={id: value, ...})` whenever >1 interrupt
> is pending"* — `:461`

Two functions, each 226+ lines with 54+ branches, encoding the same protocol for
talking to LangGraph. The difference that justifies two functions is a *sink*.

#### Consequence

- A LangGraph resume-protocol change must be applied twice, in two of the four
  most branch-dense functions in the codebase.
- Voice, CLI, and daemon interrupt behavior can drift without any signal.
- This is the concrete mechanism behind `F-T03`: because the framework's
  streaming protocol is handled inline in two places rather than behind one
  adapter, the framework is twice as expensive to abstract later.

#### Direction

One driver loop that yields events; the CLI's printing behavior becomes a
consumer of that generator. `astream_agent` collapses to roughly:

```python
async def astream_agent(...) -> str:
    async for ev in astream_agent_iter(..., ask_handler=_stdin_ask):
        renderer.handle(ev)
    return renderer.final_text()
```

The CLI already has a renderer package (`cli/render/`) with exactly this shape,
so the sink side of the refactor is largely built.

---

## F-D04

### Two competing schema-ownership mechanisms

**Severity:** **P1**
**Location:** `storage/pg/migrations.py` (866 lines) vs `CREATE TABLE` in 16 modules

#### Claim

The system has both a centralized versioned migration file *and* per-store
`CREATE TABLE` DDL, and neither is authoritative. Where a table is defined
depends on which backend it is for and when it was written.

#### Evidence

**Central:** `storage/pg/migrations.py` — 866 lines, a numbered forward-only
sequence (referenced in project notes up to v18).

**Distributed:** 16 modules carry `CREATE TABLE` statements —
`context/artifacts.py`, `context/summary_store.py`, `context/transcript_store.py`,
`daemon/task_registry.py`, `daemon/usage.py`, `memory/store.py`, `skills/store.py`,
`storage/base.py`, `storage/events/schema.py`, `storage/feedback_store.py`,
`storage/interrupts.py`, `storage/sessions/sqlite_impl.py`, `storage/voice_store.py`,
`todoboard/store.py`, `visuals/store.py`, `mcp_servers/deepface/store.py`.

**Two migration frameworks.** `BaseSqliteStore` provides its own versioning —
`_SCHEMA_VERSION`, `_SCHEMA_SQL`, `_META_TABLE`, `_migrate()`
(`storage/base.py:95-168`) — with a per-store version counter, entirely separate
from the Postgres migration numbering. So `todo_cards` is at SQLite schema
version *N* and Postgres migration *v17* simultaneously, with no relationship
between the numbers.

#### Consequence

- **No single answer to "what is the schema?"** Reconstructing the todo schema
  means reading `todoboard/store.py` `_SCHEMA_SQL`, its `_migrate` steps, *and*
  the relevant slice of the 866-line Postgres migration file.
- **Drift between backends is invisible.** Add a column to the SQLite `_SCHEMA_SQL`
  and forget the Postgres migration: it works locally, fails in production, and
  no test or type catches the gap.
- **The two version counters cannot be reconciled.** There is no way to ask
  "are these two backends at the same logical schema?"

#### Direction

One schema definition per domain, in the domain module, from which both
dialects are emitted (naturally paired with [ADR-002](adr/ADR-002-storage-mapper-layer.md)).
Interim, if ADR-002 is deferred: a conformance test that introspects both
backends and asserts identical logical columns per table. That test is cheap and
would have caught every historical drift.

---

## F-D05

### Four hand-maintained lists of the same provider set

**Severity:** **P2**
**Location:** `core/config.py:99-509`, `:520-545`, `:546-549`,
`llm/providers/__init__.py:28-36`

The provider roster is written out four times: as 13 settings dataclasses, as a
13-branch `if`-chain, as a prose list inside a `RuntimeError` message, and as the
`_PROVIDERS` tuple. Analysed as an OCP violation in
[`F-S06`](02-findings-solid.md#f-s06).

Recorded separately because the *error-message* copy (`config.py:546-549`) is a
distinct and especially fragile class of duplication: a user-facing list of
supported providers maintained by hand, with nothing connecting it to the actual
set. It is guaranteed to drift, and its drift is invisible until a user reports
a confusing error.

---

## F-D06

### Agent stack assembly duplicated across CLI, daemon, and tinker

**Severity:** **P1**
**Location:** `cli/agent_stack.py:131` (`build_agent_stack`, 270 lines, 17 params),
`daemon/bootstrap.py:291` (`build_daemon`, 927 lines),
`agents/tinker/agent.py:45` (`build_tinker_stack`, 182 lines, 12 params)

#### Claim

Above the three *builders* of `F-D01` sit three *stack assemblers* that each
independently decide which stores to construct, which retrieval indices to wire,
and how to connect them.

#### Evidence

- `cli/agent_stack.py:48` — `_build_retrieval_stores(skill_registry)`, a
  CLI-private version of retrieval wiring that `build_daemon` performs inline at
  `bootstrap.py:389-399, 656-689`.
- All three construct a `SkillRegistry`, resolve a skill store, build an
  `Embedder`, and wire injectors — with different null-handling and defaults in
  each.
- 19 deferred imports in `agent_stack.py`, 17 in `bootstrap.py` (M4) — both
  fighting the same load-order constraints, separately.

#### Consequence

There are three answers to "how is a YUYUTSAVA agent assembled", and the correct
one depends on the entry point. A change to retrieval wiring must be made in
three places whose only shared abstraction is the `build_*` function they each
call at the end.

Combined with `F-D01`, the true replication factor for agent assembly is
**3 stacks × 3 builders**, with the pairing determined by convention.

#### Direction

One `AgentStackBuilder` parameterized by profile (`cli` / `daemon` / `tinker`),
consuming the `AgentSpec` of [ADR-001](adr/ADR-001-agent-build-pipeline.md).
The profile differences are genuine but small — they belong in a config object,
not in three functions.

---

## F-K01

### 12 functions over 200 lines; peak cyclomatic 116

**Severity:** **P1**
**Location:** see [01 § M2](01-evidence-and-metrics.md#m2--function-complexity)

#### Claim

Complexity in this codebase is concentrated in a small number of very large
functions, past the point where correctness can be established by reading.

#### Evidence

30 functions exceed 100 lines; 12 exceed 200. The five worst by branch count:

| Branches | Lines | Function |
|----------|-------|----------|
| 116 | 740 | `converse` — `daemon/web/routers/converse.py:311` |
| 58 | 927 | `build_daemon` — `daemon/bootstrap.py:291` |
| 58 | 226 | `astream_agent` — `core/streaming.py:593` |
| 54 | 228 | `astream_agent_iter` — `core/streaming.py:363` |
| 38 | 330 | `run_chat_repl` — `cli/commands/chat_repl.py:639` |

#### Consequence

A function with 116 branch points has a path count no reviewer enumerates and no
test suite covers. Practically:

- Code review of these functions degrades to reviewing the diff, not the
  function. Interaction effects with the other 700 lines go unexamined.
- Any bug in them is a bisect-and-print exercise; the state space defeats
  reasoning.
- They are un-unit-testable, so they are tested (if at all) end-to-end, which
  makes the tests slow — and the project has explicitly noted heavy import tests
  as something to avoid.

#### Note

This is deliberately ranked P1, not P0. Long functions are a *symptom* here:
`build_daemon` is long because of `F-S04`, `converse` because of `F-S11`,
`astream_*` because of `F-D03`. Fixing the structural findings shrinks these
functions as a side effect. **Attacking length directly, before the structural
fixes, would produce arbitrary splits that make the code harder to read, not
easier.**

---

## F-K02

### Parameter lists of 32, 30, 25, and 25

**Severity:** **P1**
**Location:** `core/engine.py:431` (32), `:1003` (30), `daemon/web/app.py:73` (25),
`daemon/web/server.py:14` (25)

#### Claim

Sixteen functions take more than ten parameters. The worst take more than thirty,
nearly all optional and defaulting to `None`.

#### Evidence

`build_cli_deepagent` — 32 parameters, of which 27 are keyword-only optionals:
`bash_timeout_sec`, `execution_mode`, `docker_settings`, `local_settings`,
`permission_check`, `search_config`, `checkpointer`, `subagents`,
`async_subagents`, `async_host_url`, `remote_async_subagents`,
`async_task_mirror`, `async_max_concurrent`, `async_host`,
`async_host_attachment`, `artifact_store`, `summary_store`, `memory_store`,
`transcript_store`, `context_settings`, `compaction_model`, `skill_store`,
`transcript_index`, `mcp_tools`, `cap_enforcer`, `budget_tokens`, `usage_store`,
`prefs_store`, `runtime_settings`, `extra_tools`.

#### Consequence

- **Valid combinations are unknowable.** With 27 optional parameters there are
  ~2²⁷ nominal combinations; a small number are valid, and *which* is encoded only
  as runtime `ValueError`s scattered through the body (e.g. `async_subagents`
  requires `async_host_url`, `:646`).
- **Silent misconfiguration.** Omit `prefs_store` and you get a working agent
  that ignores user preferences — no error, no warning.
- **Call sites are unreadable.** A 20-keyword-argument call communicates nothing
  about intent.
- **Every new capability widens the signature**, so the three builders of
  `F-D01` drift further apart with each addition.

#### Direction

A parameter object with validated construction — `AgentSpec` in
[ADR-001](adr/ADR-001-agent-build-pipeline.md). Illegal combinations become
constructor errors at the definition site instead of runtime errors deep in a
build.

---

## F-K03

### Import placement is load-bearing behavior

**Severity:** **P1**
**Location:** package-wide; concentrated in `core/engine.py` (68 deferred imports)

#### Claim

23.7% of internal imports are deferred inside function bodies (220 of 929). In
`core/engine.py` this reaches 68 — the module cannot import its own dependencies
at module scope.

#### Evidence

M4, plus explicit acknowledgement in the source:

```python
# core/engine.py:511-513
# Imported here, not at module scope: `yuyutsava.llm` imports core.config,
# whose package __init__ re-exports this module — a module-level import would
# close that loop. Same reason model_name_of is imported lazily below.
```

15 comments across the package name cycles directly (M4).

#### Consequence

- **The import graph is not the dependency graph.** Static analysis, IDE
  navigation, and architecture-lint tools all see a cleaner structure than
  exists. A dependency-rule linter would pass while the cycles remain.
- **Moving a statement can break the program.** The system works partly because
  imports sit at particular points inside function bodies — a property no test
  asserts and no reviewer checks.
- **Cost is paid per call.** Minor, but `_context_middleware` re-imports
  compaction and offload modules on every invocation (`:352-353`).

Note that deferred imports are a *legitimate* tool for genuinely optional
dependencies — `llm/base.py:require()` uses lazy import correctly, to keep
provider SDKs optional. The finding is about the ~200 uses that exist to evade
cycles, not the handful that exist to keep extras optional.

#### Direction

Extract `yuyutsava/ports/` — abstract protocols with no internal imports — so
both sides of every cycle depend on it and on nothing else. Then enforce with an
import-linter contract in CI so the ratio cannot regress. Shared with `F-S05`.

---

## F-K04

### Dead and half-built structure left in place

**Severity:** **P2**
**Location:** see [01 § M6](01-evidence-and-metrics.md#m6--dead-and-unapplied-structure)

#### Claim

The package contains an empty layer, a stale abstraction docstring, a
deliberately-unregistered 167-line tool, and a back-compat alias kept past its
stated life. Each transmits a false signal about the intended architecture.

#### Evidence

| Artifact | Signal it sends | Reality |
|----------|-----------------|---------|
| `daemon/web/repositories/` (empty) | "routers → services → repositories" | Routers call stores directly; `converse.py` is 740 lines |
| `storage/base.py:10-12` — *"No store inherits from it yet"* | The base class is unused | 12 stores inherit from it |
| `agents/orchestrator/spawn.py` — 167-line `make_spawn_subagent_tool` | A spawn capability exists | Deliberately never registered (`engine.py:823`) |
| `core/engine.py:756` — `build_agent = build_cli_deepagent`, *"for one cycle"* | Temporary shim | Still present |

#### Consequence

A newcomer reads `repositories/` and infers a layering that does not exist; reads
`storage/base.py` and concludes the base class is aspirational when it is load-bearing;
finds `spawn_subagent` and wires it, reintroducing an abandoned design.

Project memory records that `spawn_subagent` is abandoned and must never be
registered — **that knowledge lives outside the repository**, which is precisely
the failure mode. A 167-line unregistered tool with a comment is an invitation.

#### Direction

Cheap and immediate, independent of every other finding: delete
`repositories/` or fill it; delete `spawn.py` (git preserves it) or add a
module-level `DEPRECATED — DO NOT REGISTER` banner with the rationale; correct
`storage/base.py`'s docstring; remove the alias. Roughly an hour of work that
measurably improves onboarding.
