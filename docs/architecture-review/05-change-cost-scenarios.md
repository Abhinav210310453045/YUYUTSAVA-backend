# 05 — Change-Cost Scenarios

*The P0 lens. SOLID/DRY/KISS are proxies; **cost-to-extend** is the thing they
proxy for. This document measures the real cost of realistic changes, then views
the same codebase from four different perspectives.*

Every scenario is traced against the actual code. File:line references are
verifiable; no cost is estimated by feel.

---

## Scenario A — Add a new master agent

*"We want a `ResearchAgent` master: its own system prompt, its own tool set, its
own workspace, driven by the daemon."*

### Path today

| Step | Action | Cost |
|------|--------|------|
| 1 | Copy `build_tinker_agent` (`core/engine.py:1003`) → `build_research_agent` | ~250 lines |
| 2 | Decide, by reading three functions, which of the 30 parameters apply | 30 decisions |
| 3 | Re-derive the capability matrix (`F-S02`) — does it need `SubagentGateMiddleware`? `PrefsInjector`? `FilesystemPromptOverrideMiddleware`? | undocumented |
| 4 | Copy the injector block (`:1108-1131`) | ~24 lines |
| 5 | Copy the async-subagent block + its `ValueError` (`:1174-1195`) | ~22 lines |
| 6 | Copy the middleware-ordering comment and hope the order is right | — |
| 7 | Write a stack assembler (`F-D06`) or extend `build_agent_stack` | ~180 lines |
| 8 | Wire into `build_daemon` (`bootstrap.py:291`) | inline edit to a 927-line function |
| 9 | Thread a bundle field through `DaemonSubsystems` | 1 edit |

**~475 lines, ~85% copied, four modules touched, one 927-line function edited.**

### The real cost is not the lines

Step 3 is the expensive one and it does not appear in any estimate. The
capability matrix in [`F-S02`](02-findings-solid.md#f-s02) shows that the three
existing builders already disagree on six capabilities. A fourth builder must
choose a position on each — with no documentation, no default, and no test that
would catch a wrong choice. **The most likely outcome is silent inconsistency:
a research agent that ignores user preferences because `PrefsInjector` was in the
CLI builder but not the one that got copied.**

### After [ADR-001](adr/ADR-001-agent-build-pipeline.md)

```python
RESEARCH = AgentSpec(
    role="research",
    prompt=render_research_prompt,
    tools=[ToolFamily.SEARCH, ToolFamily.CONTEXT, ToolFamily.MEMORY],
    policies=DEFAULT_MASTER_POLICIES,          # the matrix, as data
    injectors=[Injector.MEMORY, Injector.SKILLS, Injector.PREFS],
    workspace=WorkspacePolicy.PER_ROLE,
)
```

**~15 lines, zero copied, one file.** Capability choices become explicit and
reviewable in a diff. Cost falls by roughly **97%**.

---

## Scenario B — Add a new persisted domain

*"We want to persist user-defined workflows: CRUD, thread-scoped, retained 30
days."*

### Path today

| Step | Action | Location | Cost |
|------|--------|----------|------|
| 1 | Define the model dataclass | new module | ~40 |
| 2 | Write `WorkflowStore(ABC)` | new module | ~30 |
| 3 | Write `SqliteWorkflowStore` — `_SCHEMA_SQL`, `_migrate`, row-mapper, N methods | same module | ~180 |
| 4 | Write `PgWorkflowStore` — same N methods, `%s` params, `to_timestamp()`, `_PG_*_COLS` | same module | ~150 |
| 5 | Add the Postgres migration | `storage/pg/migrations.py` (866 lines) | ~25 |
| 6 | Add a backend branch | `bootstrap.py:350-443` — the 14th | ~4 |
| 7 | Decide failover: bare conditional or `RoutedStore`? (`F-S07`) | `bootstrap.py` | undocumented |
| 8 | Thread through `DaemonSubsystems` + `OrchestratorDeps` as `object \| None` (`F-S05`) | 2 modules | ~4 |
| 9 | **Add to `_PG_CHILD_TABLES`** | `storage/purge.py:79` | 1 line |
| 10 | **Add to `_STATE_TABLES`** (SQLite purge) | `storage/purge.py:210` | 1 line |
| 11 | Add a retention sweep method | `storage/sweeper.py:368-414` | ~20 |
| 12 | Write tools if agents use it | `*/tools.py` | ~60 |

**~515 lines across 6–8 modules, of which ~150 are a second implementation of
logic already written in step 3.**

### The two silent traps

**Steps 9 and 10 are the dangerous ones.** `_PG_CHILD_TABLES`
(`storage/purge.py:79-91`) is a hardcoded 11-entry tuple; SQLite has its own
parallel list. **Forget either and session deletion silently orphans your rows.**

That is not a cosmetic bug. `purge_session` is the user-facing *"delete this
session"* path. A forgotten line means user data survives a deletion request —
a data-retention failure, produced by a hardcoded list with nothing enforcing
its completeness, in a module unrelated to the one you were working in.

**Step 7 has no right answer available to you.** Nothing documents why visuals,
feedback, and todos fail over to SQLite while memory, skills, and events do not.
You will copy whichever neighbour you happened to read.

### After [ADR-002](adr/ADR-002-storage-mapper-layer.md)

```python
@domain(table="workflows", thread_scoped=True, retention_days=30)
@dataclass(frozen=True)
class Workflow:
    workflow_id: str
    thread_id: str
    name: str
    spec: dict
```

One implementation over the dialect adapter; purge and sweep derive from
`thread_scoped` and `retention_days` **by registry, not by hand-edited list**.

**~120 lines, one module.** Cost falls by ~75%, and steps 9–11 — the ones that
cause data bugs — stop existing.

---

## Scenario C — Add a new LLM provider

*"Add Fireworks AI."*

This is the **best-case** scenario in the codebase, and it is worth studying
precisely because it shows the target state.

### Path today

| Step | Action | Location |
|------|--------|----------|
| 1 | `FireworksSettings` dataclass + `from_env` | `core/config.py` (42 importers) |
| 2 | Branch in the `if`-chain | `core/config.py:520-545` |
| 3 | **Update the error-message provider list** | `core/config.py:546-549` |
| 4 | `FireworksProvider(Provider)` | `llm/providers/fireworks.py` |
| 5 | Add to `_PROVIDERS` | `llm/providers/__init__.py:28` |
| 6 | Add the pip extra | `pyproject.toml:89-104` |

**~90 lines, 4 files.**

### What this scenario proves

Steps 4–5 are excellent: one module, one tuple entry, exactly as
`llm/providers/__init__.py` promises. Steps 1–3 are the same concept implemented
in a completely different style, in the highest-fan-in module in the system
(`F-S06`).

**The same team, on the same concept, produced both the best and the worst
pattern in the codebase.** The capability to build the right structure is
present. What is missing is the follow-through of migrating the old mechanism
after the new one landed — the `llm/` refactor added a registry without removing
what it replaced.

Step 3 deserves emphasis: a **user-facing list of supported providers**,
maintained by hand, with nothing linking it to the actual set. It will drift, and
the drift surfaces as a confusing error message for a user who did everything right.

---

## Scenario D — Swap or upgrade the agent framework

*"deepagents 0.7 is out" — or — "we need to evaluate an alternative."*

### Path today

| What must change | Scale |
|------------------|-------|
| 14 middleware classes (`F-T01`) | 14 modules |
| 4 `create_deep_agent` call sites (`F-T03`) | `core/engine.py` |
| 2 streaming loops, 226+228 lines, 54+58 branches (`F-D03`) | `core/streaming.py` |
| `interrupt()` calls in tool + policy code (`F-T06`) | 3 modules |
| `BaseChatModel` references (`F-T02`) | 18 modules |
| `AgentState` / `ModelRequest` / `ModelResponse` in signatures | 14 modules |
| `BaseSubAgent.as_deepagents_subagent_spec` | 1 module |
| `DockerSandboxBackend(BaseSandbox)` — 513 lines | 1 module |
| Checkpointer plumbing (`BaseCheckpointSaver`) | 8 modules |

**64 of 350 modules import a framework directly (18.3%).** There is no seam;
the estimate for a framework swap is effectively "rewrite the agent layer."

### The realistic risk is not a swap — it is an upgrade

Nobody is going to swap frameworks. But `F-T04` shows that a *minor upgrade* can
change agent behavior with **no error and no test failure**: the filesystem
prompt block silently stops being stripped, or the `general-purpose` override
silently stops applying and a subagent's capability scope silently widens.

**And `F-T05` shows there is no upper version bound to prevent that upgrade from
happening automatically.**

### The proportionate response

Full framework independence (Phase 4) is a large investment that may never pay
off. The 80/20 is Phase 0:

- version ceilings (hours),
- three characterization tests asserting the *observable outcomes* of the three
  internal dependencies (about a day),
- a scheduled CI job that resolves without the lock and runs them.

That converts the three silent failures into three red tests, and it is
**independent of every other item in this review** — worth doing on its own
merits even if nothing else here is ever actioned.

---

## Scenario E — Onboard a new engineer

*"Ship a small feature in week one."*

### Where they get stuck, in order

1. **`build_daemon`, 927 lines, 58 branches** (`F-S01`). The only description of
   how the system fits together is statement order inside one function.
2. **Three agent builders that disagree** (`F-S02`). "Which one do I follow?"
   has no answer; the differences are undocumented and non-obvious.
3. **`object | None` everywhere** (`F-S05`). "What can I pass as `memory_store`?"
   IDE navigation is dead at exactly the seams a newcomer most needs to follow.
4. **False signals** (`F-K04`). They see the empty `repositories/` directory and
   infer a layering that does not exist. They find the 167-line
   `make_spawn_subagent_tool` and wire it in — reintroducing an abandoned design
   whose abandonment is recorded only outside the repository.
5. **Import order is load-bearing** (`F-K03`). They move an import to the top of
   `core/engine.py` for tidiness and break the process, with an error that points
   nowhere useful.
6. **The purge tables** (Scenario B, steps 9–10). They add a domain, do not know
   `storage/purge.py:79` exists, and ship an orphaned-data bug that no test
   catches.

Points 4 and 6 are the tell: **the system's most important invariants live
outside the code** — in project memory, in comments, in one engineer's head.

### Cheapest high-value fixes for onboarding

Independent of the structural work, roughly a day total:

- Delete `daemon/web/repositories/` or fill it (`F-K04`).
- Delete `agents/orchestrator/spawn.py` or add a `DEPRECATED — DO NOT REGISTER`
  module banner explaining why (`F-K04`).
- Fix the stale `storage/base.py:10-12` docstring (12 stores now inherit from it).
- Add an `ARCHITECTURE.md` under `yuyutsava/` that states the layering rule and
  names the three god functions as known debt with links to this review.

---

## Four points of view on the same codebase

Same evidence, four readers, four different verdicts. All four are correct.

### The feature developer

> *"Everything takes longer than it should, and I can't say why."*

Their experience is Scenarios A and B: real work is 15 lines, delivered work is
500, and 85% of it is copied. They cannot articulate the problem because each
individual copy is *locally reasonable* — the neighbouring code does the same
thing. The cost is invisible per-change and enormous in aggregate.

**Their metric:** copied lines per feature. Currently ~85% for agents, ~30% for
stores.

### The reviewer

> *"I can review the diff. I cannot review the change."*

A 20-line addition to `build_daemon` is reviewable as text and unreviewable as
behavior — it lands in a 927-line function with 58 branches, and its interaction
with the rest is not derivable by reading. Same for the 740-line, cx-116
`converse`. Reviews degrade to style checks on the diff.

**Their metric:** the fraction of a change's blast radius visible in its diff.
For the god functions: near zero.

### The operator

> *"Behavior differs between dev and prod and I don't know why."*

`F-S10`: SQLite twins run `BEGIN IMMEDIATE` with retry-on-busy; Postgres twins
have neither. Dev is SQLite; prod is Postgres. A concurrency bug may not
reproduce across them.

`F-S07`: during a Postgres outage, todo writes fail over to SQLite and reconcile
later; memory writes raise into agent code. One outage, two behaviors, no
documentation of which stores do which.

**Their metric:** can I predict behavior under partial failure? Currently no —
the answer is per-store and unwritten.

### The architect

> *"The good patterns exist. They stopped at the boundary of the hot paths."*

`llm/`, `retrieval/`, `platform/` are correct. `BaseSubAgent` is correct. Adding
a *sub*agent is genuinely cheap.

But *master* agents, *persisted domains*, and *process composition* — the three
paths where all growth actually happens — have no abstraction at all. So the
good patterns do not reduce the cost of change, because change does not go
through them.

**Their metric:** does the cost of the Nth feature approach a constant? Currently
no. It grows: each new master agent widens the capability matrix, each new
domain lengthens the hardcoded purge lists, each new subsystem adds branches to
`build_daemon`.

---

## Cost summary

| Scenario | Today | After Phases 1–3 | Reduction |
|----------|-------|------------------|-----------|
| A — new master agent | ~475 lines, 4 modules, 85% copied | ~15 lines, 1 module | **~97%** |
| B — new persisted domain | ~515 lines, 6–8 modules, 2 silent traps | ~120 lines, 1 module, 0 traps | **~75%** |
| C — new LLM provider | ~90 lines, 4 files, 4 duplicated lists | ~50 lines, 2 files | **~45%** |
| D — framework upgrade | unbounded, 3 silent failure modes | bounded, 3 red tests | *risk*, not lines |
| E — onboarding | ~6 structural traps | ~1 | qualitative |

**The pattern across all five: the cost is not in writing the feature. It is in
correctly re-deriving decisions the system has already made but never wrote
down.** Every fix in [06-remediation-plan.md](06-remediation-plan.md) is aimed at
that specific cost — turning re-derived decisions into declared data.
