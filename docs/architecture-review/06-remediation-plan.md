# 06 — Remediation Plan

A sequenced plan with the **creation flow** for each phase: what gets built, in
what order, how it lands without a big-bang merge, and how it is verified.

**Governing constraints** (from how this project actually works):

- The system is in active daily use. **No phase may require a flag day.**
- Every phase lands incrementally; old and new coexist behind an adapter until
  the last call site moves.
- Work happens on a feature branch; commits are local. No pushes unless asked.
- Heavy import tests (full pytest, app-importing WebSocket tests) are avoided —
  verification favours fast standalone checks. Every gate below respects that.

---

## Sequencing rationale

```
Phase 0 ── Freeze the bleeding ──┐  (independent; do first, do regardless)
                                 │
Phase 1 ── Agent build pipeline ─┤  (self-contained; highest leverage)
                                 │
Phase 2 ── Storage mapper ───────┤  (largest mass; needs no Phase 1 output)
                                 │
Phase 3 ── Composition root ─────┘  (REQUIRES 1 and 2 — they remove its branching)
                                 │
Phase 4 ── Framework boundary ────  (optional, strategic; do last or never)
```

Three things drive this order:

1. **Phase 0 is independent and urgent.** It addresses the only findings that can
   silently degrade production behavior (`F-T04`). It should start today and
   blocks nothing.
2. **Phase 3 must come last of the structural three.** `build_daemon` is long
   *because* of the 13 backend branches (Phase 2) and the three builders
   (Phase 1). Splitting it first means splitting it twice.
3. **Phases 1 and 2 are parallelizable** — they touch disjoint modules
   (`core/engine.py` vs `*/store.py`). Two engineers can run them concurrently;
   they meet in `bootstrap.py` at Phase 3.

**Phases 1–3 are justified by cost-of-change alone.** Phase 4 is a strategic bet
on framework independence and requires a separate decision.

---

## Phase 0 — Freeze the bleeding

**Addresses:** `F-T04`, `F-T05`, `F-T07`, `F-K04`
**Effort:** ~2 days · **Risk:** none · **Blocks:** nothing
**Do this first, and do it whether or not the rest of the plan is ever actioned.**

### Why first

Three failure modes currently change agent behavior with **no error, no test
failure, and no log line**, triggered by a routine dependency upgrade — and
nothing prevents that upgrade. Every hour spent elsewhere is an hour of exposure.

### Creation flow

**Step 0.1 — Version ceilings** (1 hour)

```toml
# pyproject.toml
deepagents>=0.6.3,<0.7          # tight: F-T04 depends on its internals
langchain>=1.3,<2
langchain-core>=1.4,<2
langgraph>=1.2,<2
langchain-openai>=1.1.11,<2
# … and each provider extra
```

**Step 0.2 — Characterization tests** (1 day) — `test/framework_contract/`

One test per internal dependency. Each asserts an **observable outcome**, never a
mechanism, so it survives refactors and only fails when behavior actually
changes.

| Test | Asserts | Catches |
|------|---------|---------|
| `test_filesystem_block_stripped` | Rendered CLI system prompt contains no `"## Filesystem Tools"` | `F-T04` #1 |
| `test_general_purpose_override_applied` | The resolved `general-purpose` spec is ours — probe for a marker string only our prompt carries | `F-T04` #2, #3 |
| `test_docker_backend_satisfies_protocol` | `DockerSandboxBackend` still implements the backend protocol | `F-T04` #3 |
| `test_middleware_hooks_present` | Each of the 14 policies exposes the hook it registers | `F-T01`/`F-T07` churn |

These build graphs but never call a model — fast, no network, no billable API.

**Step 0.3 — Make silence loud** (2 hours)

`FilesystemPromptOverrideMiddleware` logs at WARNING when it strips nothing.
Stripping nothing is never the intended outcome, so a silent no-op should never
be silent.

**Step 0.4 — Normalize framework imports** (1 hour)

All 16 middleware imports move to the public `langchain.agents.middleware` path
(10 currently reach into `.types` — `F-T07`). Finish the `before_model` →
`wrap_model_call` migration so one hook generation is in use.

**Step 0.5 — Remove false signals** (2 hours, `F-K04`)

- Delete `daemon/web/repositories/` (empty) or fill it.
- Delete `agents/orchestrator/spawn.py`, or add a module banner:
  `DEPRECATED — DO NOT REGISTER. The orchestrator delegates via general-purpose. See ADR-00X.`
- Fix `storage/base.py:10-12` — 12 stores now inherit from it.
- Remove the `build_agent` alias (`core/engine.py:756`).

**Step 0.6 — Scheduled upgrade canary** (2 hours)

Weekly CI job: resolve dependencies *ignoring the lock*, run
`test/framework_contract/`. An incompatible release becomes a scheduled red
build, not a mid-feature surprise.

### Exit criteria

- [ ] Every framework dependency has an upper bound
- [ ] 4 characterization tests, green, running in < 30s
- [ ] Deliberately breaking each internal dependency turns exactly one test red
- [ ] One middleware import path; one hook generation
- [ ] No empty directories; no unregistered tool without a banner

---

## Phase 1 — Agent build pipeline

**Addresses:** `F-D01`, `F-S02`, `F-K02`, `F-D06`, partially `F-T03`
**Effort:** ~2 weeks · **Risk:** medium (behavior-preserving refactor of the hot path)
**Design:** [ADR-001](adr/ADR-001-agent-build-pipeline.md)

### Target

Three 250-line builders → one declarative `AgentSpec` + one composition pipeline.
Scenario A drops from ~475 lines to ~15.

### Creation flow

**Step 1.1 — Extract the capability matrix as data** (2 days)

*Before writing any pipeline code*, make the current behavior explicit. Read all
three builders and encode the six-column matrix from
[`F-S02`](02-findings-solid.md#f-s02) as a literal table in code:

```python
# core/agent_profiles.py — descriptive first, prescriptive later
CLI_PROFILE = AgentProfile(
    policies=[TOOL_FILTER, FILESYSTEM_PROMPT, VOICE_STYLE, SUBAGENT_GATE],
    injectors=[MEMORY, SKILLS, CONVERSATION, PREFS],
    permission=Permission.OPTIONAL,
    execution_modes=[LOCAL, DOCKER],
)
ORCHESTRATOR_PROFILE = AgentProfile(...)
TINKER_PROFILE = AgentProfile(...)
```

This step ships **on its own**, changing no behavior. Its value is that the
undocumented divergence becomes a reviewable artifact — and the team can then
decide, deliberately, which divergences are intentional.

> **Expect to find bugs here.** The matrix will contain differences nobody chose,
> e.g. tinker lacking `SubagentGateMiddleware` whose docstring
> (`engine.py:526-528`) gives a correctness argument that applies to tinker too.
> Fix those as separate, individually reviewable commits — not silently inside the
> refactor.

**Step 1.2 — Build the pipeline beside the old builders** (4 days)

`core/agent_pipeline.py` — assembly as an ordered list of composable steps:

```python
PIPELINE = [
    resolve_model, resolve_checkpointer, resolve_backend,
    build_policy_stack,      # was: middleware list assembly
    build_injector_stack,    # was: the triplicated block
    build_tool_registry, build_subagent_specs,
    render_system_prompt, compile_graph,
]

def build_agent(spec: AgentSpec) -> AgentBundle:
    ctx = BuildContext(spec)
    for step in PIPELINE:
        step(ctx)
    return ctx.bundle
```

Middleware ordering — currently a comment repeated three times — becomes an
assertion inside `build_policy_stack`.

**Step 1.3 — Reimplement the old builders as thin adapters** (2 days)

```python
def build_cli_deepagent(workspace_root, settings, **kw) -> AgentBundle:
    return build_agent(AgentSpec.from_legacy_cli(workspace_root, settings, **kw))
```

Signatures unchanged; every existing call site keeps working. **This is the step
that makes the phase incremental** — the new pipeline is exercised by all
existing traffic before anything migrates.

**Step 1.4 — Equivalence gate** (2 days)

Before deleting anything, prove the pipeline produces equivalent graphs:

```python
def graph_fingerprint(bundle) -> dict:
    return {
        "middleware": [type(m).__name__ for m in bundle.agent.middleware],
        "tools": sorted(t.name for t in bundle.agent.tools),
        "subagents": sorted(s["name"] for s in bundle.agent.subagents or []),
        "prompt_sha": sha256(bundle.system_prompt),
    }
```

Assert old and new fingerprints match for all three profiles × their flag
combinations. Fast, no model calls.

**Step 1.5 — Migrate call sites, delete the old builders** (2 days)

Then add a fourth profile as the proof: a spec-only agent, ~15 lines, no copying.

### Exit criteria

- [ ] `core/engine.py` under 400 lines
- [ ] No function over 15 parameters
- [ ] Capability matrix is data, in one file, with a test asserting each profile
- [ ] Fingerprint equivalence green for all three profiles
- [ ] A new master agent demonstrably costs < 30 lines

---

## Phase 2 — Storage mapper layer

**Addresses:** `F-D02`, `F-D04`, `F-S04`, `F-S07`, `F-S10`, `F-S12`, `F-S03`
**Effort:** ~4 weeks · **Risk:** medium-high (data path) — mitigated by strict per-domain increments
**Design:** [ADR-002](adr/ADR-002-storage-mapper-layer.md)

### Target

51 store classes → ~17 implementations + 2 dialect adapters. Scenario B drops
from ~515 lines to ~120, and the two silent data-loss traps stop existing.

### Creation flow

**Step 2.1 — Conformance suite first** (1 week) ← *the load-bearing step*

**Before touching any store**, write one behavioral test suite per domain,
parameterized over both backends:

```python
@pytest.mark.parametrize("store", [sqlite_todo_store, pg_todo_store])
async def test_assign_note_touches_parent_card(store): ...
```

This does three jobs at once:

1. **Documents the contract** each twin pair is supposed to satisfy — which is
   currently written nowhere.
2. **Finds existing divergence** (`F-S10` predicts transaction-semantics
   differences; expect more).
3. **Becomes the safety net** for the collapse. A domain is only migrated once
   its conformance suite passes against both old implementations.

> Do not skip or shorten this step. It is what converts Phase 2 from a risky
> rewrite into a mechanical one. If the plan is cut for time, **cut phases, not
> this step.**

Postgres tests need a live PG; gate them on a marker so the SQLite half stays
fast and always-on.

**Step 2.2 — Dialect adapters** (3 days)

```python
class Dialect(Protocol):
    def placeholder(self, i: int) -> str:        # "?" | "%s"
    def now_expr(self, param: str) -> str:       # "?" | "to_timestamp(%s)"
    def upsert(self, table, cols, conflict) -> str
    async def transaction(self, conn): ...       # BEGIN IMMEDIATE | pool tx
```

The transaction method is what dissolves `F-S10`: one policy, two
implementations, applied by the shared store base rather than per twin.

**Step 2.3 — Migrate one domain end to end** (1 week)

Pick **visuals** — small (359 lines), already `RoutedStore`-wrapped, with on-disk
side effects that will surface any hidden assumptions. Prove the whole pattern on
it, including purge and sweep integration, before touching anything else.

**Step 2.4 — Domain registry replaces the hardcoded lists** (3 days)

The step that eliminates Scenario B's silent traps:

```python
@domain(table="visuals", thread_scoped=True, retention_days=30)
class VisualRecord: ...
```

`storage/purge.py:79` `_PG_CHILD_TABLES` and its SQLite twin become **derived**
from the registry. Add a test asserting every `thread_scoped=True` domain is
reachable by purge — so forgetting is impossible rather than merely discouraged.

**Step 2.5 — Migrate remaining domains, ~2 per iteration** (2 weeks)

Order by ascending risk: visuals → feedback → usage → summaries → transcripts →
artifacts → todos → memory → skills → events → sessions.

Each domain ships independently; conformance suite green before and after.

**Step 2.6 — Unify backend selection and failover policy** (2 days)

One `StoreFactory` resolves the backend once (`F-S04` → 13 branches become 1).
Failover becomes a declared per-domain policy, so `F-S07`'s "which stores survive
a Postgres outage" is finally *data* rather than an accident of wiring.

**Step 2.7 — Narrow the consumer signatures** (2 days, `F-S03`)

The per-domain ABCs at `storage/events/abc.py` already exist. Change consumers to
declare the narrow interface they use (`store: DecisionStore`). `Store` survives
as a construction-time aggregate; only declared types move. Low risk, high clarity.

### Exit criteria

- [ ] Conformance suite per domain, green on both backends
- [ ] Store class count ≤ 40 (from 71)
- [ ] `CREATE TABLE` in ≤ 2 modules (from 16)
- [ ] Purge/sweep table lists derived from the registry; completeness test green
- [ ] One backend-selection site in `bootstrap.py` (from 13)
- [ ] Failover policy declared per domain

---

## Phase 3 — Composition root

**Addresses:** `F-S01`, `F-S05`, `F-S08`, `F-K01`, `F-K03`, `F-D06`
**Effort:** ~2 weeks · **Risk:** medium · **Requires:** Phases 1 and 2
**Design:** [ADR-003](adr/ADR-003-composition-root-modules.md)

### Creation flow

**Step 3.1 — Extract `yuyutsava/ports/`** (4 days) ← *fixes the root cause*

A dependency-free package containing **only** abstract protocols:
`MemoryStore`, `ArtifactStore`, `SummaryStore`, `TranscriptStore`,
`SkillStore`, `CapEnforcer`, `AsyncTaskMirror`, `ArtifactSink`.

Rule: `ports/` imports nothing from `yuyutsava` except `ports` itself.

This is what makes `F-S05` fixable. Both sides of every cycle import `ports`;
neither imports the other; the ~10 `object | None` fields become real types and
most of the 220 deferred imports (`F-K03`) collapse.

Enforce with an `import-linter` contract in CI so the ratio cannot regress.

**Step 3.2 — Type the dependency contracts** (2 days)

`OrchestratorDeps` and `BaseSubAgent.__init__` declare real types from `ports`.
Delete the defensive `getattr(deps, "async_subagents", None)` reads — the
declared field can be trusted once it is typed.

**Step 3.3 — Split `build_daemon` into subsystem builders** (4 days)

```python
async def build_daemon(opts) -> DaemonSubsystems:
    storage   = await build_storage(opts)              # was :340-450
    retrieval = await build_retrieval(opts, storage)   # was :650-700
    agents    = await build_agents(opts, storage, retrieval)
    web       = await build_web(opts, storage, agents)
    return DaemonSubsystems(storage, retrieval, agents, web)
```

Each builder is independently testable and under 150 lines. This is only
tractable now because Phase 2 removed the 13 backend branches and Phase 1
removed the builder duplication.

**Step 3.4 — Retire the global singletons** (3 days, `F-S08`)

Replace the five `set_default_*`/`get_default_*` pairs with an explicit
`AppContext` threaded through call sites. `purge_session(ctx, session_id)` is
barely less convenient than `purge_session(session_id)` and is honest about the
four stores it touches.

**Step 3.5 — Unify the three stack assemblers** (3 days, `F-D06`)

One `AgentStackBuilder` parameterized by profile, consuming Phase 1's
`AgentSpec`.

### Exit criteria

- [ ] No function over 200 lines in `daemon/bootstrap.py`
- [ ] Zero `object | None` dependency fields
- [ ] Deferred-import ratio under 8% (from 23.7%)
- [ ] Import-linter contract in CI, green
- [ ] No `set_default_*` globals
- [ ] One stack assembler

---

## Phase 4 — Framework boundary *(optional, strategic)*

**Addresses:** `F-T01`, `F-T02`, `F-T03`, `F-T06`
**Effort:** ~4 weeks · **Risk:** high · **Requires:** Phases 1 and 3
**Design:** [ADR-004](adr/ADR-004-framework-boundary.md)

### Decide before starting

Phase 4 buys framework independence. That is only worth 4 weeks if at least one
holds:

- a realistic prospect of changing or supplementing the agent framework;
- policy logic that must run outside a graph (batch, replay, evaluation);
- policy tests that are currently too slow or fragile because they need a
  framework;
- repeated pain from framework API churn beyond what Phase 0 contains.

**If none hold, stop after Phase 3.** Phase 0 already caps the acute risk at a
fraction of the cost. Recording "we chose not to" as an ADR is a legitimate and
useful outcome.

### Creation flow (if approved)

1. **`yuyutsava/policy/`** — our `Policy` protocol in our own types, plus one
   generic `LangChainPolicyAdapter(AgentMiddleware)`. 14 framework subclasses → 1.
2. **`ModelHandle`** — the `F-T02` fix. Do this one **regardless of Phase 4**:
   it is ~50 lines, deletes `model_name_of`'s six-way duck-typing, and gives
   capability checks a home.
3. **`AskUser` port** — domain code calls `await ctx.ask(prompt)`; the LangGraph
   adapter implements it with `interrupt()`. Makes the consent core testable
   without a graph (`F-T06`).
4. **`Agent` protocol** — `astream`/`ainvoke`; the deepagents graph becomes the
   default implementation rather than the definition (`F-T03`).
5. **One driver loop** — collapse `astream_agent`/`astream_agent_iter` behind a
   sink (`F-D03`). The CLI renderer package already has the right shape.

---

## Effort and value summary

| Phase | Effort | Risk | Reduces cost of | Do it? |
|-------|--------|------|-----------------|--------|
| **0** | 2 days | none | Silent framework breakage | **Yes — start now** |
| **1** | 2 weeks | medium | New master agents (~97%) | **Yes** |
| **2** | 4 weeks | med-high | New domains (~75%) + data-loss traps | **Yes** |
| **3** | 2 weeks | medium | Onboarding, testability, cycles | **Yes** |
| **4** | 4 weeks | high | Framework independence | **Decide separately** |

**Phases 0–3: ~8.5 weeks** for a system where the three most common changes cost
45–97% less and two classes of silent data bug become structurally impossible.

---

## Tracking

Re-run the [01 § metric summary](01-evidence-and-metrics.md#metric-summary-table)
at each phase exit and record the row. The targets:

| Metric | Now | After 0 | After 1 | After 2 | After 3 |
|--------|-----|---------|---------|---------|---------|
| Store classes | 71 | 71 | 71 | ~35 | ~35 |
| Functions > 200 lines | 12 | 12 | 8 | 6 | ~2 |
| Functions > 10 params | 16 | 16 | 6 | 5 | ~3 |
| Max cyclomatic | 116 | 116 | 116 | 110 | ~40 |
| Deferred-import ratio | 23.7% | 23.7% | 18% | 15% | < 8% |
| `CREATE TABLE` modules | 16 | 16 | 16 | ≤ 2 | ≤ 2 |
| `object \| None` deps | 10 | 10 | 6 | 6 | 0 |
| Deps with ceilings | 0 | **all** | all | all | all |

> `converse` (cx=116) is untouched by Phases 0–3 — it is `F-S11`, an independent
> extraction. Worth scheduling alongside Phase 3 by whoever owns the voice path;
> it does not block anything else.
