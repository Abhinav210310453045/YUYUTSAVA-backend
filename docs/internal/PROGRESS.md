# Execution Progress

Live status of the [remediation plan](06-remediation-plan.md). **Updated as work
lands, not as it is planned.**

**Rule for this file:** nothing is marked `DONE` on the strength of having been
written. `DONE` means executed and verified, with the verification recorded in
the log at the bottom.

| Legend | Meaning |
|--------|---------|
| `TODO` | Not started |
| `WIP` | In progress, not yet verified |
| `DONE` | Landed **and verified** |
| `DEFERRED` | Deliberately postponed; reason given |
| `REVISED` | Plan changed after contact with the code; see note |

---

## Phase 0 — Freeze the bleeding ✅ COMPLETE

**Goal:** no dependency upgrade can silently change agent behavior.
**Started / completed:** 2026-08-08 · **Risk taken:** none (no behavior changes)

| Step | Task | Status | Notes |
|------|------|--------|-------|
| 0.0 | Baseline: what already exists? | `DONE` | Found 1 of 3 tripwires existed — **and it was dead**. See A below |
| 0.2a | Repair filesystem-block tripwire | `DONE` | Patch target fixed; brittle assertion replaced with an invariant |
| 0.2b | General-purpose override tripwire | `DONE` | 2 tests: name collision + dispatch mechanism |
| 0.2c | Docker backend protocol tripwire | `DONE` | First attempt was **vacuous**; caught by negative control. See C below |
| 0.2d | Backend-factory tripwire | `DONE` | **Not in the original plan** — added after discovering B below |
| 0.1 | Version ceilings | `DONE` | 20/20 framework deps capped; 3 undeclared direct deps declared |
| 0.3 | Warn when the override strips nothing | `DONE` | Warn-once-per-instance; silent when healthy |
| 0.4 | Normalize framework imports | `DONE` | 10 `.types` sites → public path. **Hook migration cancelled** — see D |
| 0.5 | Remove false signals | `DONE` | Empty dir removed, 2 docstrings corrected, `spawn.py` banner added |
| 0.6 | Upgrade canary | `DONE` (adapted) | No CI exists → shipped `scripts/verify_framework_contract.py` instead |
| — | Pre-existing test failure found and fixed | `DONE` | See E below |

### Exit criteria — all met

- [x] Every framework dependency has an upper bound (20/20)
- [x] Tripwires exist for all three silent-failure seams, plus a fourth
- [x] **Each tripwire verified to go RED when its contract is broken** (negative control)
- [x] One middleware import path across all 16 sites
- [x] No empty directories; no unregistered tool without a banner
- [x] All 340 modules still import; touched-area tests pass

---

### What Phase 0 found that the review did not

#### A. The one existing tripwire had been dead for months

`test/test_filesystem_prompt_override.py` was well written and self-described as
an "upgrade tripwire" — but it monkeypatched `engine.chat_model`, which the
`llm/` provider refactor removed (the builders now import `chat_model` lazily
from `yuyutsava.llm` inside the function body). It died at `AttributeError`
before asserting anything.

**A test that exists and does not run is worse than a missing test**: it reads as
coverage in the file listing and its failure is invisible. This is why every
tripwire added in Phase 0 was negative-controlled rather than merely observed to
pass.

Its second assertion was also brittle — it pinned an *exact* tool list
(`{write_todos, task, tool_search}`), so the later `vis_*` / `artifact_*`
families broke it. Replaced with the invariant that actually matters: no
suppressed prefix family reaches the model, and `tool_search` does.

#### B. deepagents 0.7.0 will break 3 of the 4 agent build paths ⚠️

Surfaced by a `DeprecationWarning` during the first successful test run:

> `Passing a callable (factory) as backend is deprecated and will be removed in
> deepagents==0.7.0. Pass a BackendProtocol instance directly instead.`

`core/engine.py` passes a callable at `:735` (CLI local), `:995` (orchestrator)
and `:1224` (tinker). Only the Docker path passes an instance. **On deepagents
0.7, every non-Docker agent in the system stops building.**

This is `F-T05` (no ceilings) materializing as a dated, confirmed break rather
than a hypothetical. It converts the `<0.7` pin from caution into necessity, and
adds a concrete migration task — tracked as **carry-over C1** below.

#### C. My first protocol tripwire was vacuous — the negative control caught it

`test_docker_backend_satisfies_protocol` originally used `hasattr`. But
`DockerSandboxBackend` *inherits* from the protocol
(`DockerSandboxBackend → BaseSandbox → SandboxBackendProtocol → BackendProtocol → ABC`),
so `hasattr` is `True` for every protocol member whether or not anything
implements it. The test passed and asserted nothing.

Rewritten to check `__abstractmethods__` — the set Python itself computes for
unimplemented abstract members, which is exactly what determines whether the
class can be instantiated. **The plan's insistence on negative controls paid for
itself on its first use.**

#### D. A review finding was wrong, and acting on it would have broken compaction

`F-T07` claimed the codebase straddles "two generations" of the middleware hook
API, citing `before_model` as superseded by `wrap_model_call`, and the Phase 0
checklist included migrating the two `before_model` users.

**Verification showed both hooks are current API with different semantics:**
`before_model` returns *state updates*; `wrap_model_call` wraps the *request*.
`context/compaction.py` uses `before_model` because compaction replaces
`state["messages"]` — something `wrap_model_call` structurally cannot do. The
migration would have broken compaction.

Migration cancelled; finding corrected and downgraded to P2 in
[04](04-findings-thirdparty-coupling.md#f-t07). The import-path half of that
finding was correct and is fixed.

#### E. A pre-existing test failure, hiding a feature nobody was testing

`test/context/test_offload_middleware.py::test_small_result_passthrough` was
failing before any Phase 0 change (verified by stashing). Not a bug: the
middleware gained `always_offload_prefixes` (default `("ws_",)`) which
force-offloads `ws_*` results regardless of size, and the test's "small result"
case used `ws_search`.

Consequence: the **size-gated passthrough path had no coverage at all**, and the
`always_offload` behaviour had none either. Fixed the stale test to use a
size-gated tool name and added a test pinning the always-offload behaviour.
5 tests with 1 failure → 6 passing.

---

### Carry-over items created by Phase 0

| ID | Item | Priority | Where it lands |
|----|------|----------|----------------|
| **C1** | Migrate `backend=` from callable factories to `BackendProtocol` instances, then lift the `<0.7` pin | **High** — blocks all future deepagents upgrades | Phase 1 (`resolve_backend` pipeline step, [ADR-001](adr/ADR-001-agent-build-pipeline.md)) |
| **C2** | Migrate the 2 real `build_agent` callers to `build_cli_deepagent`, drop the alias | Low | Phase 1 |
| **C3** | No tripwire for the LangGraph interrupt/resume protocol (`F-T06`) | Medium | Phase 4 (needs the driver-loop consolidation) |

---

## Phase 1 — Agent build pipeline ✅ COMPLETE

**Started:** 2026-08-08 · Carry-overs **C1** and **C2** absorbed and closed.

| Step | Task | Status | Notes |
|------|------|--------|-------|
| C1 | `backend=` factories → `BackendProtocol` instances | `DONE` | deepagents 0.7 blocker cleared; deprecation warnings gone |
| C2 | Migrate `build_agent` callers, drop alias | `DONE` | CLI entry point verified working |
| 1.1 | Capability matrix as data | `DONE` | `core/agent_profiles.py` + 6 conformance tests |
| 1.4 | Fingerprint equivalence gate | `DONE` | **Built early** — see revision below |
| 1.2a | Extract triplicated injector block | `DONE` | Fingerprint byte-identical before/after |
| 1.2b | Extract async-subagent block | `DONE` | Required extending the gate to 9 configs first — see finding I |
| 1.2c | Extract shared context-tools block | `DONE` | All 9 configurations identical |
| 1.3 | Builders read the profile | **`DONE`** | Tool families, todo scope, um_* namespace **and now policies** are profile-driven via `_policy_middleware`. Finding AP |
| 1.5 | Prove a 4th agent costs < 30 lines | `DONE` | `test/core/test_fourth_master_cost.py` — **21 lines of data**, guarded against regression |

### `REVISED` — two deliberate deviations from the written plan

**1. Step 1.4 (equivalence gate) was moved *before* 1.2, not after.**
The plan sequenced it after the pipeline was built. Building the safety net
first is strictly better and mirrors the reasoning already applied to Phase 2's
conformance suite: a refactor without its verification harness is unverifiable
work. The gate was then negative-controlled (scrambled order → 18 failures,
dropped middleware → detected, removed tools → named) *before* any production
code was touched.

**2. Step 1.2 is being executed as incremental extractions, not a
parallel build-then-swap.**
ADR-001 describes building a full `PIPELINE = [...]` beside the old builders and
cutting over. Instead, each triplicated block is being extracted into a
profile-driven helper one at a time, with the fingerprint gate proving each
extraction byte-identical. Rationale:

- every step is independently verified and independently revertible;
- there is never a moment where two implementations must be kept in sync;
- it converges on the same destination without a risky cutover.

The end state is unchanged. Only the path is safer.

### Findings from Phase 1 so far

**AP. Policies are now data too — and the gate caught a live regression writing it.**

Each of the three masters wrote its own policy list out in full. They agreed,
but only by hand: a fourth master meant a fourth copy, which is the cost ADR-001
exists to remove. `_policy_middleware(profile, phase, …)` builds the list from
`profile.policies`.

**Ordering deliberately does not live in the profile.** A profile is a *set*,
and a set has no order — so the pipeline order is two separate tuples,
`_POLICY_ORDER_PRE` (before the context controllers) and `_POLICY_ORDER_POST`
(after). Budget and usage must observe **post-compaction** token counts, so they
cannot be hoisted; putting order in the profile would have made it a list, and a
list invites "just append it".

**Profile ≠ wiring.** A policy is built when the profile declares it *and* its
argument is supplied. Those answer different questions: the profile says this
master *may* have a budget (a capability); `budget_tokens=None` says nobody wired
one here (the standalone CLI). Conflating them is why this was assembled by hand
three times.

**The fingerprint gate caught a real behaviour change mid-refactor.** My first
version guarded `SubagentGateMiddleware` on `runtime_settings is not None` — but
the original constructed it unconditionally and passed `None` through. That would
have silently dropped the middleware for the standalone CLI. Caught immediately,
across 9 configurations; fingerprint byte-identical after the fix.

**A source-scan test was retired, and I checked that was safe.**
`test_policies_match_builders` scanned each builder for policy class names —
impossible now that they live in one shared helper, exactly as happened to the
injector scan earlier. The replacement is `test_agent_fingerprint.py`, which
inspects the **assembled** stack rather than the source.

Worth recording how that was verified: my first negative control *passed*, which
would have meant the gate did not cover policies and retiring the scan had lost
coverage. It turned out the control itself was vacuous — the edit used the wrong
indentation and never applied. Re-run correctly, removing `Policy.VOICE_STYLE`
from `CLI_PROFILE` fails the gate. A control that does not fire is a claim about
the control first, not about the thing under test.

**F. The capability matrix is worse than the review reported: 12 divergences, not 6.**
`core/agent_profiles.py` now records every one. Beyond the six the review found,
execution surfaced: `vis_*` tools (orchestrator lacks them), `tr_*` and `db_*`
(orchestrator lacks), `inherit_env` (orchestrator runs unattended), and —
found by the fingerprint harness, invisible to source inspection —
**`skill_injector_scope`: the CLI's `SkillInjector` is unscoped**, so the CLI
master retrieves skills authored for *any* agent, while the orchestrator and
tinker scope to their own. Whether that breadth was intended is unrecorded; it
is preserved as-is rather than silently "fixed".

**G. The fingerprint harness caught two gaps in itself.**
First: with no stores wired, the offload/compaction/budget/usage middleware never
materialise, so the initial fingerprint could not have detected a break in the
documented ordering rule. Fixed by adding a fully-wired variant — six
configurations (three profiles × bare/wired). Second: middleware were recorded by
class name only, which would have missed a dropped or reordered *injector* inside
`RetrievalInjectionMiddleware` — precisely what step 1.2a then refactored. Fixed
by digesting container internals.

**H. Consolidation made static tests weaker and runtime tests stronger.**
Extracting the injector block moved the injector class names out of the three
builders and into one helper, which broke the source-scanning conformance check —
it could no longer tell the profiles apart. Rather than weakening the check,
injector verification moved to the fingerprint, where the chain is inspected at
runtime including its *order* and the SkillInjector *scope*. Net: the tests got
more precise, not less.

**I. The gate would have rubber-stamped step 1.2b.**
After extracting the async-subagent block, the fingerprint gate passed — but it
proved nothing: none of the six configurations wired async subagents, so the
extracted code never executed. Added a third configuration per profile
(`:async`, with a background subagent, a deliberately `supports_async=False` one
to exercise the opt-in filter, a remote peer, and a task mirror), taking the gate
from 6 to 9 configurations.

Generalised lesson, now applied twice: **a green gate only means something for
code paths the gate actually executes.** Both times the omission was an optional
dependency defaulting to `None`.

**J. Whole-phase equivalence, measured against pre-Phase-1 code.**
Rather than trusting the incremental checks, the full engine was reverted via
`git stash` and all 9 fingerprints regenerated from the original code. Across
every configuration the *only* difference is:

```
backend: 'function' -> 'LocalShellBackend'
```

— exactly and only the intended C1 fix. Middleware lists including order, every
tool set, all sync and async subagent specs, prompt hashes and checkpointers are
identical. That is machine-checked evidence that 1.2a, 1.2b and C2 are
behaviour-preserving and that C1 is the sole intended behavioural change.

**K. Divergences are now classified by intent, not just counted.**
User confirmed the orchestrator's missing `tr_*` / `db_*` / `vis_*` is a
deliberate architectural boundary — it is a plain router that handles events and
delegates all execution to subagents. The matrix treated that identically to
accidental drift, which would have let ADR-001 "normalise" a real design decision
away. `REVIEWED_DIVERGENCES` now records a rationale per divergence, and the
ratchet tracks **unreviewed** count (4), not the total (12). Resolving one means
*deciding*, not necessarily changing code.

**L. All four unreviewed divergences resolved — 12 → 8, unreviewed 4 → 0.**
Decided 2026-08-08. Three were resolved by making the capability uniform, one by
recording that the mechanisms differ but the effect does not.

| Divergence | Decision | Effect |
|------------|----------|--------|
| `skill_injector_scope` | **Every agent gets only its own skills** | All three masters now scope to their role. The store filter is `agent IS NULL OR agent = <role>`, so each keeps *shared* skills and stops pulling skills another agent authored for itself. The CLI was the leak |
| `supports_remote_async` | **Tinker may use subagents for any task, sync or async** | `remote_async_subagents` now threaded through `build_tinker_agent`; tinker can delegate to peer daemons like its siblings |
| `FilesystemPromptOverrideMiddleware` | **Strip it in the orchestrator too** | It has no filesystem tools at all, so the block advertised nothing and burned ~700 cache-prefix tokens per task |
| `SubagentGateMiddleware` | **Tinker honours the runtime toggle** | Follows from tinker using subagents: its bundle is cached per card and can outlive a toggle change, so a disabled subagent stayed reachable from the board |
| `PrefsInjector` | **No change — recorded as by-design** | The orchestrator gets prefs as a build-time `prefs_block`; a fresh graph is built per task, so build-time injection is equivalent |

The gate confirmed these are the *only* behavioural changes: no tool set moved,
no prompt hash changed, no subagent was lost. Per-configuration diff:

```
cli:wired/async     SkillInjector -> SkillInjector(cli)
orchestrator:*      + FilesystemPromptOverrideMiddleware
tinker:*            + SubagentGateMiddleware
tinker:async        + peer-daemon remote subagent
cli:bare            unchanged
```

**The ratchet is now the useful one:** `unreviewed_divergences()` must stay at
**0**. Non-zero means a master gained or lost a capability without anyone
deciding it should. The remaining 8 are architectural decisions with recorded
rationale, and ADR-001 must **preserve** them.

### Measured effect so far

| | Before | After |
|---|---|---|
| `build_cli_deepagent` | 321 lines | 291 |
| `build_orchestrator` | 242 lines | 217 |
| `build_tinker_agent` | 246 lines | 224 |
| Injector construction sites | 3 | **1** |
| Async-wiring sites | 3 | **1** |
| Host-URL guards | 3 | **1** |
| Async-spec dict-shape checks | 3 | **1** |

`engine.py` grew 1319 → 1409 lines: the two shared helpers carry ~140 lines of
documentation and explicit parameters that the triplicated inline blocks did not.
**Line count is the wrong measure here** — the goal is single-point-of-change,
and four rules that lived in three places each now live in one.

**M. The profile became prescriptive, and that made two test classes circular.**
`_shared_master_tools` now *reads* `AgentProfile` — which families to wire, the
`todo_*` scope, the `um_*` namespace. That is the ADR-001 goal, but it broke the
static conformance checks in a subtle way: the helper wires a family *because*
the profile declares it, so asserting "profile says X, source mentions X" could
never fail. A passing circular test is worse than no test.

Both moved to runtime checks against the tools actually bound onto the built
agent (`ProfileDrivenToolFamilies`), which is non-circular. That suite also now
asserts the routing boundary directly: **the orchestrator must never bind
`tr_*` / `db_*` / `vis_*`** — the precise "helpful normalisation" a
uniform-builder refactor could introduce by accident.

Static checks survive only for the families the builders still choose
independently (`sk_*`, `ws_*`, `tr_*`, `db_*` via `_build_tool_registry_and_tools`).

**N. Optional-dependency blindness, third occurrence.**
The orchestrator's `ws_*` and `sk_*` never appeared in fingerprints because the
harness supplied neither `search_config` nor `skill_registry`. Same root cause as
the unwired context stores (finding G) and the unwired async subagents
(finding I): **an optional dependency defaulting to `None` silently removes a
code path from coverage.**

Fixed; the orchestrator fingerprint went 11 → 22 tools. Worth internalising as a
standing rule for this codebase, where optional-and-defaulting-to-None is the
dominant wiring idiom: *when a harness reports less than you expect, suspect the
harness before the code.*

### Phase 1 acceptance — ADR-001's headline claim, measured

`test/core/test_fourth_master_cost.py` defines a hypothetical fourth master
(`research`) entirely as data and drives the shared helpers with it.

| | Review baseline | Now |
|---|---|---|
| Declaring a master | ~475 lines, ~85% copied | **21 lines of data** |
| Modules touched | 4 | 1 |
| Undocumented capability decisions | 12 divergences to re-derive | 0 — the profile *is* the decision |

The test also guards the property that makes this hold: the three shared helpers
must stay **role-agnostic**. It fails if any of them regains an
`if role == "cli"` branch, because at that point the profile alone stops being
sufficient and the copy-paste pressure returns.

What is still per-builder is genuinely role-specific: prompt rendering, workspace
layout, checkpointer selection, and the orchestrator's `ask_user`/`recall` pair.

### Carry-over status

| ID | Item | Status |
|----|------|--------|
| C1 | `backend=` instances, deepagents 0.7 | ✅ **Closed.** Pin stays `<0.7` until 0.7 ships and the contract suite passes against it |
| C2 | `build_agent` alias | ✅ **Closed** |
| C3 | No tripwire for LangGraph interrupt/resume | Open — Phase 4 |

## Phase 2 — Storage mapper layer ✅ COMPLETE

**Started:** 2026-08-08. Step 2.1 (conformance suite) is the load-bearing one —
ADR-002 says cut phases before cutting it. **It has already paid for itself: two
real production bugs found and fixed on its first run.**

| Step | Task | Status | Notes |
|------|------|--------|-------|
| 2.1a | Structural twin conformance | `DONE` | `test/storage/test_twin_conformance.py` — 19 twin pairs, 8 tests, no DB required |
| 2.1b | Behavioural conformance (both backends) | `DONE` (rollback) | **Unblocked** — PG 16.14 up on `127.0.0.1:5433` (note: 5433, not 5432). `test/storage/test_rollback.py` runs 9 tests against both live backends and skips PG cleanly when it is down |
| 2.2 | Dialect adapters | `DONE` | `yuyutsava/storage/dialect.py` — 173 lines, shared by every future domain |
| 2.3 | Migrate first domain (visuals) | `DONE` | `UnifiedVisualStore`; twins deleted after 40 parity assertions passed on both live backends. **-50 code lines/domain**, break-even at ~1.2 domains — see finding S |
| 2.1c | Transactional + rollback on BOTH providers | `DONE` | User request 2026-08-08. See finding O |
| 2.4 | Domain registry replaces hardcoded purge/sweep lists | `DONE` | `yuyutsava/storage/domains.py` — 31 domains declared; purge lists derived, not written. Found a **third** leak (finding R) |
| 2.5a | Cut visuals over + delete twins | `DONE` | `bootstrap.py` + the CLI fallback both run the unified store; twin count 19 → 18 |
| 2.5b | Migrate remaining domains | **`DONE`** | **18 of 19 migrated.** Only `usage` remains, and it is **paused by decision** (finding U), not unfinished. Twin pairs **19 → 1**; `KNOWN_ASYMMETRIES` empty. Findings T, V, AC, AE, AF, AG, AH, AI, AJ, AK, AL, AM |
| 2.6 | Unify backend selection + failover policy | `DONE` | `storage/factory.py` — store-selection branches **13 → 0**; spillover now a declared policy. Closes `F-S04` + `F-S07` |
| 2.7 | Narrow consumer signatures (`F-S03`) | **`DONE`** | 13 Protocols, **all but one wired to a real consumer**; orphan-role ratchet added. Finding AQ |

### Findings — both are live bugs, now fixed

**O. 8 Postgres write methods were not atomic; their SQLite twins were.**

> ### ⚠️ Correction — this was first reported as "16"
>
> The initial count came from a keyword regex over method source, which
> over-counted badly: it matched the verb in method *names* (`async def delete`),
> in *comments* (`ON DELETE CASCADE`, `ON DELETE SET NULL`), and **both halves of
> a single `INSERT … ON CONFLICT DO UPDATE` upsert**.
>
> Re-measured by parsing the AST and counting only `execute()` calls whose SQL
> literal *starts with* a mutating verb: **8 genuinely non-atomic methods**, not
> 16. The other 8 edits were applied to already-atomic single-statement methods —
> harmless (a lone statement inside an explicit transaction is still correct) and
> aligned with the "make everything transactional" instruction, but they were
> **not bug fixes** and should not have been counted as such.
>
> The ratchet now uses the AST counter, so the number it reports is real.
> Lesson: *a regex over source code is a heuristic, not a measurement* — and it
> is worth re-deriving any number before putting it in a report.

`PgPool.connection()` is **autocommit** — each statement commits on its own.
`PgPool.transaction()` is the explicit atomic wrapper, and its docstring says
exactly when to use it (`storage/pg/pool.py:126-136`). But 16 multi-statement
write methods used `connection()`, so a failure part-way through left a partial
write. Every one of their SQLite twins *is* atomic, via
`BaseSqliteStore._run_write` → `BEGIN IMMEDIATE`.

**This only breaks in production.** Development runs SQLite, where the same code
path is atomic.

Worst case found — `PgFeedbackStore.upsert`:

```python
async with self._pool.connection() as conn:   # AUTOCOMMIT
    DELETE FROM message_feedback WHERE ...    # commits immediately
    INSERT INTO message_feedback ...          # separate commit
```

A failure between the two destroys the user's prior rating and never writes the
replacement. Others include `TodoStore.delete_card` (2 DELETEs → orphans),
`ConsentGrantStore.put` (INSERT + UPDATE → a half-written consent grant), and
`TodoStore.assign_note`.

The 8 real ones: seven `PgTodoStore` methods (`add_note`, `add_objective`,
`add_attachment`, `assign_note`, `update_note`, `update_objective`,
`update_attachment` — each a write plus a parent-card touch) and
`PgFeedbackStore.upsert`.

Fixed by switching to `self._pool.transaction()`. `TransactionSemantics` in the
conformance suite now fails on any new occurrence, on **both** backends.

**Proven against live Postgres 16.14**, not just asserted — a two-write method
that raises before completing:

```
OLD path connection()  -> 2 rows survived   (partial write persisted)
NEW path transaction() -> 0 rows survived   (rolled back)
```

This is `F-S10` quantified: reported as a qualitative LSP concern, it was
**8 concrete atomicity bugs** with a measurable production consequence.

**Both providers are now transactional with rollback** (2026-08-08 request):

| Path | Before | Now |
|------|--------|-----|
| `BaseSqliteStore._run_write` | `BEGIN IMMEDIATE` + commit; rollback only *implicitly*, by closing the connection | Explicit `conn.rollback()` on any `BaseException`, so cancellation cannot leave a partial write either |
| `SqliteEventsBackend` | `execute()` commits **per statement**; no multi-statement helper existed at all | New `transaction()` context manager — `BEGIN IMMEDIATE`, commit, explicit rollback, sharing the same write lock |
| `PgPool.transaction()` | Existed and was correct — but 8 methods used autocommit `connection()` instead | All multi-statement writes use it; `connection()`'s non-rollback behaviour is now pinned by a test so nobody "simplifies" back to it |

**Honest scope note on the SQLite half.** The explicit `rollback()` is
*hardening, not a bug fix*. Verified by removing it and re-running: the tests
still pass, because closing the connection already discards an open transaction.
It makes the code match a docstring that already promised rollback and stops the
guarantee resting on driver close semantics. The `SqliteRollback` docstring says
this outright, so nobody reads those green tests as proof of something they do
not test.

**P. Deleting a session left the conversation text on disk.**

`message_feedback` rows store `user_text` and `assistant_text` verbatim. The
table sits outside the thread-hub FK graph, so neither `_STATE_TABLES` /
`_PG_CHILD_TABLES` nor the Postgres cascade reached it — and `FeedbackStore` had
no `delete_for_thread` method at all. So "delete this session" left the user's
messages behind.

This is precisely the trap ADR-002 predicts from hardcoded purge lists
([05, Scenario B, steps 9–10](05-change-cost-scenarios.md#scenario-b--add-a-new-persisted-domain)):
adding a domain means remembering to edit a list in an unrelated module, and
forgetting is silent.

Fixed: `delete_for_thread` added to the interface and both twins, wired into
`purge_session` beside the visuals step that already did this correctly.
Regression test in `test/storage/test_feedback_purge.py` asserts no verbatim text
survives.

**U. `usage.py` had already invented the dialect pattern — and hides a
user-visible reporting divergence. Migration PAUSED pending a decision.**

Two things surfaced while sizing `UsageStore` as the fourth migration.

*The pattern was already there.* `daemon/usage.py` carries
``_list_filters(task_id, since, ph)`` and
``_aggregate_sql(group_by, since, day_expr, ph)`` — SQL builders parameterised
by **placeholder style** and **day expression**, with each twin supplying its
own ``_DAY_EXPR``:

```
SqliteUsageStore._DAY_EXPR = strftime('%Y-%m-%d', ts, 'unixepoch')
PgUsageStore._DAY_EXPR     = to_char(to_timestamp(ts), 'YYYY-MM-DD')
```

That is a dialect adapter, invented ad hoc for one domain, before ADR-002
proposed one. Independent arrival at the same design is the strongest
validation the approach has had — and also shows the cost of *not* generalising
it: the same idea now exists twice, once here and once in `storage/dialect.py`.

*The divergence.* `add()` does not behave the same on the two backends:

| | SQLite | Postgres |
|---|---|---|
| empty `thread_id`/`task_id` | stored as `''` | converted to `NULL` |
| `task_id` naming a row absent from `tasks` | **stored as-is** | **nulled** via `(SELECT task_id FROM tasks WHERE task_id = %s)` |

The nulling is an FK workaround, and the code comment names the case it hits:
the tinker recorder tags rows ``tinker:<card_id>``, which is a chat thread, not
an orchestrator task. So **on Postgres every tinker usage row loses its task
attribution; on SQLite it keeps it** — and `GET /usage?group_by=task` reports
differently depending on the backend.

**RESOLVED 2026-08-08 — Option C.** Three options were put to the user:

- **A** adopt Postgres semantics (SQLite nulls orphans too) — costs the ability
  to attribute spend to a card, on every backend;
- **B** adopt SQLite semantics (drop `llm_usage_task_fk`) — costs the guarantee
  that `task_id` joins to a real task;
- **C** change neither, and expose `thread_id` to the report.

**C was chosen, and it is the one that dissolves the problem rather than picking
a loser.** The card identity was never actually lost on Postgres: it lives in
`thread_id` (`todo:<card_id>`), written identically on both backends and under
no FK. The only real defect was that `GroupBy` did not include it.

Measured before and after, same tinker turn:

```
BEFORE   SQLITE   group_by=task   -> [('tinker:card_42', $0.05)]
         POSTGRES group_by=task   -> [('',              $0.72)]   <- swallowed

AFTER    SQLITE   group_by=thread -> [('todo:card_42',  $0.05)]
         POSTGRES group_by=thread -> [('todo:card_42',  $0.05)]   <- identical
```

Cost of the change: one enum value, one key expression, two `Literal`s on the
HTTP surface. No migration, no constraint change, no storage semantics touched —
`llm_usage_task_fk` still guarantees `task_id` names a real orchestrator task.

`test/storage/test_usage_thread_grouping.py` (11 assertions, both live backends)
pins **both halves**: the new grouping works everywhere, *and* the storage
divergence is still there. The second half matters — if a later change makes
SQLite null orphans (Option A) or Postgres keep them (Option B), that should
fail a test rather than surface as a changed cost report.

**V. A fifth divergence, found before writing a line of migration code.**

Sizing the ``events:*`` batch (7 pairs) turned up a cross-backend behaviour
difference in the *first pair read*. Every Postgres ``put`` in
``events/pg_stores.py`` carries ``ON CONFLICT (...) DO NOTHING``; three SQLite
twins used a plain ``INSERT``:

| | Postgres | SQLite (before) |
|---|---|---|
| `ConsentRuleStore.put` duplicate | silently ignored | **raises IntegrityError** |
| `ProposalStore.put` duplicate | silently ignored | **raises IntegrityError** |
| `DecisionStore.put` duplicate | silently ignored | raises (unreachable — fresh ULID) |

`ConsentRuleStore` is the one that bites — but **not for the reason first
recorded here**. An earlier version of this finding said ``rule_id`` is
"caller-chosen". It is not: the single construction site mints a fresh ULID
(``triage_loop.py:387``), so two distinct rules never collide.

The real exposure is a **retry**: re-putting the same rule object is a no-op on
Postgres and an ``IntegrityError`` on SQLite, which is the *default* backend.

That distinction changes whether ``DO NOTHING`` is the right fix, so it is worth
being exact about. It is right **here** because a collision can only mean "the
same rule, twice", and there is no update path at all — the only other mutation
is ``DELETE FROM consent_rules WHERE rule_id=?`` (``routers/rules.py:26``).
Consent rules are create-or-delete.

⚠️ ``DO NOTHING`` would be the **wrong** fix if ``rule_id`` ever became
content-derived or user-supplied: a legitimate edit would then vanish silently,
which is worse than the crash it replaced. Anyone adding "edit this rule" must
add an explicit ``DO UPDATE`` path rather than reusing ``put``.

Fixed by giving the three SQLite puts the same clause (supported since SQLite
3.24). `test/storage/test_events_idempotent_put.py` pins it, including that it
is ``DO NOTHING`` and not ``DO UPDATE`` — a repeated put must not overwrite the
existing rule, because Postgres does not.

**A first pass flagged six, and four were false positives** — they already used
SQLite's own idempotent syntax (``INSERT OR REPLACE`` / ``INSERT OR IGNORE``) or
``ON CONFLICT ... DO UPDATE``. Checking each before reporting is why this
finding says three. Same lesson as finding O, applied without needing to be
re-learned.

**W. Consent rules are append-only with newest-wins — PARKED, not a defect.**

Noticed while verifying finding V. Recorded so it is not rediscovered.

There is no edit path for consent rules: `put` (create), `list`, and
`DELETE ... WHERE rule_id=?` are the whole surface. Because `rule_id` is a fresh
ULID per call, changing your mind **appends** a second rule rather than
replacing the first, and `ConsentEvaluator.evaluate` is first-match-wins over
`ORDER BY created_ts DESC` — so the newest matching rule takes effect and it
behaves like an edit.

That is a coherent design (append-only log, newest wins). Two consequences
follow from it:

1. **Superseded rules accumulate.** `expires_ts` is set only for
   `auto_approve` (7 days); `auto_skip` rules never expire. Every re-decision
   leaves a dead rule that `evaluate` still scans on every event.
2. **Deleting the newest rule resurrects the older one.** Flip approve→skip,
   then delete the skip rule via `GET /rules` + `DELETE`, and the original
   approve rule silently takes effect again.

Neither is caused by the idempotency fix and neither is a crash. Whether they
matter is a **product** question, not a structural one: append-only-newest-wins
is fine if intended; if rules are meant to be a current-state set, then
`_add_consent_rule_for` should supersede matching rules and an edit path is
needed. **User decision 2026-08-08: park it, add edit functionality later.**

**T. The second migration found a live Postgres race — and disproved a claim I
had written into the code.**

`ThreadSummaryStore` was migrated second: smallest remaining pair, and unlike
visuals it has no on-disk side effect, so it tests whether the seam generalises
beyond the case it was designed against.

The twins allocated version numbers differently — SQLite did `SELECT MAX()+1`
then `INSERT`; Postgres used one `INSERT ... SELECT ... RETURNING`. I adopted
the Postgres form for both and wrote in the docstring that a single statement
"cannot interleave with a concurrent writer".

**That is false, and the parity suite proved it against the live server.** At
READ COMMITTED the `SELECT` inside the `INSERT` reads a transaction *snapshot*,
so two concurrent writers both see the same `MAX(version)`, both insert
`max + 1`, and one dies on `thread_summaries_pkey`. Being one statement does not
make it serialisable.

Crucially, `PostgresTwin` fails the same test — so this is a **pre-existing bug
in the shipped `PgThreadSummaryStore`**, not one the migration introduced. SQLite
never hit it because `BaseSqliteStore` serialises writes through a single lock,
which is exactly the dev-passes/prod-fails asymmetry `F-S10` describes.

Fixed in the unified store by retrying on duplicate-key (`Dialect.
is_unique_violation` — a real backend difference, so it belongs on the dialect).
Final state: `PostgresUnified` **passes**, `PostgresTwin` **fails**. The twins
were not merely matched, they were beaten.

Two things worth taking from this beyond the bug:

1. **The concurrency test only existed because I wrote it to confirm an
   assumption.** Had I not tried to verify the claim, the assumption would have
   shipped as a docstring asserting the opposite of the truth.
2. It is the strongest available argument for finishing the migration: every
   domain still on twins may hold a similar asymmetry that only production
   traffic would reveal.

**S. The first domain migration does not pay for itself — and the headline
number I first reported was the flattering one.**

Comparing implementation classes: `SqliteVisualStore` (126) + `PgVisualStore`
(85) = 211 lines, replaced by a 95-line `UnifiedVisualStore`. That "54% less" is
true and it is also **not the number that matters**.

Counting the whole change, raw lines went **up**: `visuals/` was 359 lines, now
391 (`store.py` 155 + `store_unified.py` 236). The unified module also carries a
`VisualSchema` class and factory functions the twins did not need, plus
substantial explanatory prose.

Measured properly — executable lines, docstrings and comments excluded by an AST
pass:

| | Code lines |
|---|---|
| visuals before | 264 |
| visuals after | 214 (`store.py` 78 + `store_unified.py` 136) |
| **per-domain saving** | **−50** |
| `dialect.py`, one-time | 62 |
| **break-even** | **~1.2 domains** |

So the seam has already paid for itself, but the honest projection for the
remaining 18 twin pairs is roughly **−900 code lines**, not the ~2,000 a naive
"211 → 95 × 17" extrapolation suggests.

**Line count was never the real return anyway** — it is the least important of
the three:

1. *One place to change.* A rule now lives once. The three atomicity bugs, the
   two purge leaks and the three asymmetries all came from rules living twice.
2. *Divergence becomes impossible*, not merely detectable. The conformance suite
   catches drift between two implementations; one implementation cannot drift.
3. *Cross-backend behaviour is tested once.* The parity suite is 10 assertions
   run against every backend, instead of two hand-written suites that can
   disagree about what the contract even is.

Recording this because the flattering framing was the one I reached for first,
and a report that quietly rounds in its own favour is worth less than the work
it describes.

**R. A third domain was leaking, and the registry is what found it.**

Introspecting the **live** Postgres schema (34 tables, 19 thread/session-scoped)
against what `purge_session` actually deletes exposed `pending_asks`: it carries
both `thread_id` and `session_id`, stores the agent's question (`title`, `body`)
and the user's `response`, and was **purged by nothing and swept by nothing**.
`PendingAskStore` had no delete method at all.

Third instance of one bug class — after `message_feedback` and alongside it.
That is the argument for step 2.4 in a sentence: the problem was never any
individual missing line, it was that *nothing could tell you a line was missing*.

**Fixed and, more usefully, made structural:**

- `yuyutsava/storage/domains.py` declares all 31 persisted tables with how each
  is cleaned up — `ROW_DELETE`, `STORE_METHOD` (has an on-disk side effect, so it
  must go through its store), `EXTERNAL` (the checkpointer / session store / hub
  drop owns it), or `KEEP` (deliberately survives, e.g. `memories` and the todo
  board).
- `purge.py`'s `_STATE_TABLES` / `_PG_CHILD_TABLES` are now **derived** from it.
  Verified byte-identical to the hand-maintained lists *before* anything was
  added, so the swap changed no behaviour.
- `delete_for_thread` added to `PendingAskStore` on both twins, exposed on the
  `Store` facade, wired into `purge_session`.

The value is in what `EXTERNAL` and `KEEP` buy: "this table is not in the purge
list" stops being an absence nobody noticed and becomes **a statement someone
made**, with a reason the tests require.

Three checks now hold the line, all negative-controlled:

| Check | Catches |
|-------|---------|
| `test_every_store_method_domain_is_called_by_purge` | A domain declared but never actually invoked by `purge_session` — exactly how both earlier bugs looked |
| `test_no_live_table_is_undeclared` | A migration creating a table nobody declared. Verified: creating `_undeclared_probe` makes it fail |
| `test_every_scoped_live_table_is_accounted_for` | A live table carrying `thread_id`/`session_id` with no declared lifecycle |

The live-schema checks are the ones that matter — a static list cannot catch a
table a migration created, and that is precisely how all three leaks happened.

**Q. Three twin asymmetries verified as correct, not drift.**
`PgArtifactStore.recall` and `Pg{Memory,Skill}Store.backfill_embeddings` exist on
only one backend — but all three are pgvector capabilities SQLite genuinely
cannot provide, and all three call sites are guarded. Recorded in
`KNOWN_ASYMMETRIES` with the guard that protects each, so a *new* asymmetry
fails the suite while these do not.

Worth noting the inconsistency: `recall` uses a declared `supports_recall`
property (greppable, typed, cannot be forgotten); `backfill_embeddings` uses
`getattr(store, ..., None)` probes at three call sites. The declared form is
better and the codebase already demonstrates it.

Also verified *not* a bug: the Postgres purge list covers three tables the SQLite
list does not (`artifact_chunks`, `transcript_chunks`, `interrupts`). The first
two are pgvector-only with no SQLite schema; SQLite interrupts live in a separate
DB file purged by its own step.

## Phase 3 — Composition root ✅ COMPLETE

Unblocked by 2.6 (the 13 backend branches that made `build_daemon` long are gone).

| Step | Task | Status | Notes |
|------|------|--------|-------|
| 3.1 | Extract `yuyutsava/ports/` | `DONE` | 9 dependency protocols; leaf-ness enforced by `test/test_ports_is_a_leaf.py` |
| 3.2 | Type the dependency contracts | **`DONE`** | `OrchestratorDeps` **11 → 0** untyped fields; `BaseSubAgent` 3 → 1; 3 new `ports/` Protocols. Finding AR |
| 3.3 | Split `build_daemon` into subsystem builders | **`DONE`** | **7 builders**, 606 lines named. `build_daemon` **927 → 508 lines, 58 → 17 branches**. Findings Y, Z, AB, AN |
| 3.4 | Retire the `set_default_*` globals | **`DONE`** (incremental by design) | **91 → 34 call sites.** `AppContext` seam + `purge_session`; the TODO router's 19 handlers now declare the board via `Depends`. Findings AA, AS |
| 3.5 | Unify the three stack assemblers | **`DONE`** | `StoreFactory.context_stores()` — the CLI, tinker and daemon shared one backend decision. Finding AO |

### Findings

**AT. The dialect contaminated pooled connections — found by running the daemon.**

A live boot failure, and mine:

```
W yuyutsava.prefs.runtime: runtime settings: load failed; using defaults
  File ".../storage/events/pg_stores.py", line 90, in get
    return row[0]
KeyError: 0
```

`PostgresDialect.reading()` and `.write()` set `conn.row_factory = dict_row` and
never restored it. A **pooled** connection goes back to the pool afterwards, so
every later borrower also got mappings — and anything reading `row[0]` failed at
a call site that had never touched the dialect.

**Why the whole suite missed it.** Every test opens its own `PgPool` and
exercises one kind of consumer, so a contaminated connection was never handed
on. The daemon shares one long-lived pool between the unified stores and
everything else, so it failed on the first boot after the change. *Sharing was
the precondition, and no test created it.* That is the same lesson as findings
Y and Z, in a new place: a green suite covers the paths it executes, and
"two consumers, one pool" was not one of them.

**The blast radius was much wider than the symptom.** An audit found ~14
positional reads on pool connections. Prefs merely failed *first*, at boot:

| Also affected | Consequence had it not been caught |
|---|---|
| `retrieval/pg.py` (`PgVectorSearch`) | **all** semantic recall — memory, skills, artifacts |
| `sessions/pg_impl.py` | session listing and the checkpoint-size query |
| `daemon/usage.py` | usage aggregation and reporting |
| `context/transcript_index.py`, `todoboard/recall.py` | transcript and board-note recall |

Fixed by restoring the previous factory in a `finally` — borrowing a connection
must not change it for the next borrower. Verified by contaminating a pool
deliberately and then driving all six paths.

`DialectLeavesPooledConnectionsAlone` pins it: read, write, and a **failed**
write must all leave the pool clean, plus a baseline control asserting the pool
yields tuples to begin with (otherwise the suite would pass for the wrong
reason). All four fire when the restore is removed.

**Left as-is deliberately:** the mixed row-factory convention itself. Making the
pool uniformly `dict_row` and converting ~14 positional readers is the tidier
end state, but it is a broad change with the daemon currently working — worth
doing on purpose, not as a hotfix.

**AU. My own check was scoped to the shape I happened to change.**

Second live failure from the same step, this time hitting the Artifacts tab:

```
File ".../routers/todos.py", line 391, in _require_attachment_on_card
    card = await ex.get_card(card_id)
NameError: name 'ex' is not defined
```

The rewrite replaced `get_default_exchange().` with `ex.` everywhere in the
module, then added `ex: TodoExchange = Depends(board)` to every
`@router`-decorated handler. **Three module-level helpers**
(`_require_note_on_card`, `_require_objective_on_card`,
`_require_attachment_on_card`) got the call but never the parameter — and the
two attachment-serving handlers that call them had no `ex` either.

The consistency check I wrote at the time inspected **only decorated handlers**,
because handlers were what I had edited. It reported "none" while three
functions were broken. Scoping a check to the shape you touched is how a
mechanical rewrite ships a `NameError`.

Two things changed as a result:

* the check now walks **every function in the module** and looks for any `Load`
  of the name `ex`, not just `ex.` inside a handler;
* `EveryRouteActuallyResponds` **calls every GET route** through `TestClient`.
  The signature checks all passed while the helpers were broken — only
  executing the route catches a name error in a helper body.

Both fire when a helper's parameter is removed again. The static check names the
function; the live probe returns 500 on the two attachment routes.

Worth stating plainly: two production bugs (AT, AU) came out of Phase 3 work
that the whole suite called green, and both needed the daemon actually running
to surface. The suites cover what they execute — `build_daemon` still has no
automated boot, and these are what that gap looks like in practice.

**AS. Nineteen handlers, one declared dependency (P3.4 continued).**

`daemon/web/routers/todos.py` was the largest single cluster of the service
locator: **19 handlers** each calling `get_default_exchange()` in their body.
Same three costs as `purge_session` (finding AA), at 19× the scale — the
dependency in no signature, no way to exercise a handler against a different
board, one board per process structurally.

`board()` is now a FastAPI dependency: `app.state` when the daemon installed
one, the process global otherwise. Same additive shape as `AppContext` —
unmigrated paths keep working, the honest path exists.

Overriding a board in a test is now
`app.dependency_overrides[board] = ...` rather than setting a module global and
remembering to restore it. `test_override_replaces_the_board` proves the seam by
swapping the board **with no global set**, and both negative controls fire when
a single handler is put back on the global.

**Call-site count: 91 → 55 → 34.** The drop from 91 to 55 was not this step —
it came from the Phase 2 store migrations deleting the twins that called them.

**What remains, and why it is a stopping point rather than a gap.**
34 sites across six globals, the largest being `get_default_session_store` (14).
Those live in CLI commands and web routers that each construct their own
context; migrating them is the same mechanical change repeated, with no further
design question to settle. ADR-003 scoped 3.4 as *additive by construction* —
the seam is what makes the remaining ones a chore rather than a risk, and
`GLOBAL_CONTEXT` exists precisely so they keep working meanwhile.

**AR. The dependency record now describes every dependency (P3.2).**

`OrchestratorDeps` opened Phase 3 with **11** fields typed `object | None` or
`Any`. Each was a dependency the record could not describe, so a caller had to
read the builder to learn what was expected — which is what a dependency record
exists to prevent.

The last three are done, and the interesting part is *how*: not by importing the
concrete classes, which would have pulled `async_subagents` — and with it the
LangGraph host — onto the orchestrator's import path. That coupling is why they
were `object` in the first place. Instead, three Protocols in `ports/` name what
is actually used:

| Field | Was | Now | Surface named |
|---|---|---|---|
| `context_settings` | `object \| None` | `ContextTuning` | 4 attributes |
| `async_task_mirror` | `object \| None` | `TaskMirror` | 3 of 11 methods |
| `remote_async_subagents` | `list[object] \| None` | `list[RemoteSubagentSpec]` | 4 attributes |

All three concrete classes already satisfied their Protocol **structurally** —
no inheritance, no edit to `async_subagents` or `context`, and `ports/` stays a
leaf (`test_ports_is_a_leaf.py` still green).

Guarded by `OrchestratorDepsIsFullyTyped`: reverting any field to `object`
fails, and there is a control proving the AST scan finds the fields at all. A
further test asserts the Protocols are satisfied *structurally* — inheriting one
would make `ports` non-leaf and quietly undo step 3.1.

**AQ. Seven Protocols that narrowed nothing (P2.7).**

`roles.py` declared 12 Protocols; **7 had no consumer at all** — written for
completeness and left "awaiting Phase 3", because their consumers were typed
`object` or `Any` to dodge an import cycle. A Protocol nobody annotates with is
documentation pretending to be a constraint.

Wired to the consumers that were already calling exactly those methods:

| Consumer | Was | Now |
|---|---|---|
| `daemon/ask_registry.py` | `store: Any` | `PendingAskRegistry` (3 of ~30 methods) |
| `core/policy.py` | `store: object` *"kept untyped to avoid cycle"* | `ToolCallCounter` (2 methods) |
| `web/services/decision_service.py` | `store: object` | `ProposalWriter` |
| `events/source.py` | `store: Store` | `EventPayloadWriter` (**one** method) |
| `events/tools.py` | `store: Store` ×2 | `RecallReader`, `EventPayloadReader` |

The cycle those `object` annotations dodged was real — the fix is a Protocol,
which is what `ports/` (step 3.1) made available. `EventPayloadReader` is new:
the pair had a writer and a sweeper but no reader, so the only annotation
available to `ev_fetch_event` was the whole Store.

`EventSourceContext.store` is the sharpest one: **every** `EventSource` subclass
receives that context, and the wide type advertised ~30 methods to code that
calls one.

**One role is still unwired, and it is listed rather than hidden.**
`DecisionReader`'s consumer reaches it through an attribute (`hub.store`), so
narrowing means typing the hub's field — a different change. The ratchet
exempts it by name.

Guarded by `EveryRoleHasAConsumer`, negative-controlled twice: an orphan
Protocol is caught by name, and a *stale exemption* is caught too — claiming a
role is "composed into TriageStore" fails if it is not actually in the MRO.
Plus a control proving the AST scan finds roles at all.

**AO. One backend decision, three copies (P3.5).**

The CLI stack, the tinker bundle and the daemon each wrote out the same
selection by hand — `if pg_pool is not None:` over artifacts / summaries /
transcripts — **plus** a separate two-condition guard for the transcript index
(`pg_pool is not None and embedder is not None`). Three copies of one decision,
and the daemon's copy already lived in `StoreFactory`.

`StoreFactory.context_stores()` returns all four as a `ContextStores` record;
`transcript_index()` owns the two-condition guard. The other two assemblers call
it.

Behaviour-preserving because `StoreFactory.is_postgres` is **`pg_pool is not
None`** — the CLI's exact condition, not `settings.is_postgres()`. That matters:
the CLI opens its own pool and passes `None` when it fails, so a settings-based
factory would have kept selecting Postgres stores against a dead pool. Asserted
by `test_a_dead_pool_falls_back_to_sqlite`.

12 tests. `NoStackAssemblerHandRollsTheBranch` parses each of the three
assemblers and fails if any constructs a store or a `PgTranscriptIndex`
directly — negative-controlled by putting the CLI's branch back, and with its
own control proving the AST scan actually finds the functions.

**AN. The subagent roster had five copies of one argument list (P3.3).**

The three sync subagents were constructed with the same seven keyword arguments
written out three times — then the *same two agents again*, with the same seven,
at the `ConversationManager` call site. Five hand-maintained copies, in which a
typo gave one sibling a different store and nothing caught it.

`build_subagents` shares one `**common` dict. The second construction became
`subs.make_peers()`, which mints **fresh instances** with the same dependencies —
the "fresh per graph" requirement is about object identity (a deepagent spec must
not share tool/middleware objects across graphs), not about the dependency set,
which is what made the duplication look necessary.

Extracting it also made the roster constructible in a test for the first time —
no servers, no database, no models. That bought a check on a decision that was
previously only a comment: **the TinkerAgent must not reach the sync roster.**
It is an async peer only; inline tinkering would block the conversation that
asked. 14 tests.

**AM. The TODO board — last and largest, and the contract was proved first.**

804 lines, 20 methods, 5 tables, and it holds **real user data**. So the order
was inverted: the parity contract was written first and run against the
**existing twins**, before a line of the unified store existed.

That step paid for itself immediately — four of my assumptions were wrong and
the twins told me so:

| My assumption | Reality |
|---|---|
| `"doing"` is a card status | `inbox`/`active`/`done`/`archived` |
| `assign_note(note, objective_id=…, phase=…)` | takes positional `updated_ts` too |
| `list_all_attachments` returns `(att, card)` pairs | returns bare attachments |
| `"building"` is an objective phase | `thinking`/`planning`/`doing`/`completed`/`blocked`/`abandoned` |

All 43 behaviours then passed against `SqliteTodoStore` **and** `PgTodoStore`
unchanged — so the suite describes what the board already did, not what the
rewrite happened to do. Any later failure was unambiguously a regression.

**One behaviour, one mechanism.** Postgres had four `ON DELETE CASCADE` keys
onto `todo_cards`; SQLite deleted the same four tables by hand, because
`PRAGMA foreign_keys` is off on that connection. The unified store deletes
children **explicitly on both**, leaving the Postgres keys as a safety net
rather than the mechanism — so behaviour no longer depends on which backend is
underneath, or on a constraint quietly losing its `ON DELETE`. Same for the
`SET NULL` on a note's `objective_id`. A Postgres-only test still asserts the
four cascades exist, because that is a real property worth keeping.

**A wrong assumption the tests caught in the rewrite:** `todo_cards.pinned` is
INTEGER on **both** backends, not BOOLEAN on Postgres. Passing a Python `bool`
is a type error there. Fixed, and the suite's own docstring corrected — it had
stated the wrong thing.

Sixth domain where the Postgres twin's row mappers unpacked **positionally**
(five of them here). All read by name now.

`todoboard/store.py`: **1122 → 230 lines** — the module now holds only the ABC,
the field whitelists and the store global.

87 parity tests.

**AL. Interrupts: the best-effort write contract, now actually tested.**

`interrupts` is the HITL audit log, and `record`/`resolve` run **in front of a
live user prompt**. If they raise, the user sees a crash instead of a question.
Both twins therefore caught everything, logged, and carried on — `record`
returning `""` to mean "no audit row", which callers hand back to `resolve`,
hence its empty-id guard.

That contract was load-bearing and untested. `BestEffortWrites` now drives a
store whose every write explodes and asserts `record` returns `""`, `resolve`
swallows, and `mark_orphaned_for_session` returns 0 — because a refactor turning
any of these into a raise is silent until the day the database is unavailable,
which is the worst possible day to find out.

This domain is also the **counter-example to the two-clock pattern**:
`created_at` is `DOUBLE PRECISION` and both twins already bound `time.time()`
explicitly. Four consecutive domains had the bug (AE, AH, AI, AJ); this one does
not. Recorded so the pattern is not over-generalised — and asserted, so it stays
true.

`storage/interrupts.py` went **434 → 76 lines**: the module now holds only the
record type and the ABC.

**AK. `Dialect.ts_param()` encoded a column-type assumption its name hid.** ✅ **CLOSED 2026-08-09 — migration v20.**

A flaw in the abstraction *this review introduced*, found by the eighth domain
to use it. `ts_param()` emits `to_timestamp(%s)` on Postgres — correct only
where the column is `TIMESTAMPTZ`. `interrupts.created_at` is
`DOUBLE PRECISION`, so the insert failed:

```
column "created_at" is of type double precision
but expression is of type timestamp with time zone
```

The name says "timestamp param", which reads as *backend*-dependent. It is
actually **column**-dependent, and the schema is not consistent: seven tables
store epoch seconds as `TIMESTAMPTZ`, two (`tasks`, `interrupts`) as
`DOUBLE PRECISION`.

Audited every use across the nine unified stores: **interrupts was the only
wrong one**. `tasks`, also `DOUBLE PRECISION`, happened to bind with a plain
`ph()` — correct, but by luck rather than by knowing.

**Resolved by making the schema uniform** (user decision, 2026-08-09).
Migration **v20** converts all 19 `DOUBLE PRECISION` timestamp columns to
`TIMESTAMPTZ`, so `ts_param()`/`epoch()` are now *always* the right helpers and
the per-column knowledge is gone. Live schema: **0 DOUBLE, 41 TIMESTAMPTZ.**

The split was never two odd tables — it was 22 vs 19, two conventions with no
stated principle. My first framing ("interrupts and tasks are the outliers") was
wrong and the inventory corrected it before any work started.

**Rehearsed twice before touching the live database:**

| Rehearsal | Result |
|---|---|
| Fresh DB, full v1→v20 chain | 0 DOUBLE, 41 TIMESTAMPTZ |
| v19 DB with seeded epoch rows, upgraded in place | values **bit-exact**, NULLs preserved, re-running v20 a no-op |

Each `ALTER` is guarded on `information_schema`, so the migration is idempotent
and a database created fresh from the post-v20 `CREATE TABLE` bodies is skipped.

**Three things the conversion surfaced, none of which a type change alone would
have caught:**

1. **`extract(epoch FROM …)` returns `numeric`, which psycopg hands back as
   `Decimal`.** Without a cast, a timestamp reads as `Decimal` on Postgres and
   `float` on SQLite, and `Decimal(x) == float(x)` is `False`. Stores that
   happened to wrap the value in `float()` hid it; the events decision store did
   not and compared unequal against its own input. Fixed once, in
   `PostgresDialect.epoch()` → `::float8`.
2. **`_SELECT_COLS` was doing double duty as an INSERT column list** in
   `sessions/pg_impl.py` — a coupling its own comment warned about. Splitting it
   was forced the moment the read list contained expressions rather than names.
3. **`epoch()` could not take a table-qualified column**: it emitted
   `AS {column}`, and `d.ts` is not a legal alias. It now takes an explicit
   alias.

**The one real cost, pinned rather than discovered later:** `TIMESTAMPTZ`
resolves to **1 microsecond**, so an epoch float no longer round-trips
bit-exactly (`…0348558` → `…034856`). No measurement is lost — `time.time()`
resolves to about a microsecond anyway — but exact float equality is gone, so
two parity assertions moved to a 1 µs tolerance *with the reason written at the
assertion*, and `TimestamptzResolutionIsMicroseconds` states the bound as a
property and checks that rounding cannot reorder rows written a millisecond
apart.

**Guarded by `test/storage/test_timestamp_convention.py`** (9 tests), all
negative-controlled:

| Control | Result |
|---|---|
| Add a `DOUBLE PRECISION` timestamp column | **caught**, named `table.column` |
| Remove the `::float8` cast from `epoch()` | **caught** |
| Declare `TIMESTAMPTZ` in a SQLite schema | **caught** |

Plus a ratchet on the column count (41) so an unreviewed schema change is
visible, and a control proving the SQLite AST scan actually finds schema owners
rather than passing vacuously.

Beyond the stores, this touched the spillover reconciler (`ts_cols` on 6 event
`TableSpec`s — machinery that already existed for the todo/visual tables),
`PgSessionStore`, `PgUsageStore` (`_DAY_EXPR` simplified; a separate PG read
list), and `PgPrefsBackend`. Sessions, prefs and the reconciler's generated SQL
were verified directly against live Postgres because no suite covers them.

Worth recording that **the parity suite caught this on the first run** — the
Postgres cases failed immediately and the SQLite ones passed, which is exactly
the divergence class the suites exist for. The only reason it took a debug
script to *read* was the store's best-effort contract swallowing the exception,
which is itself correct behaviour.

**AJ. Artifacts: `KNOWN_ASYMMETRIES` is now empty.**

`test_twin_conformance.py` opened Phase 2 with three declared asymmetries —
methods that existed on one backend only. Two were `backfill_embeddings`,
guarded by `getattr` probes (the bad pattern); one was `ArtifactStore.recall`,
guarded by a declared `supports_recall` property (the good one, cited in that
file as the model).

With artifacts migrated, **all three entries are gone**: the probes were deleted
in finding AI, and `recall` is no longer an asymmetry at all — one store
implements it and reports honestly whether it does anything. The dict is empty
and the ratchet keeps it that way.

`supports_recall` was kept verbatim rather than reinvented, and tightened in one
respect: it now requires **all three** of pool, embedder and the
`semantic_recall` flag. A half-configured store reports `False` instead of
failing at the first `put` — `test_recall_requires_pool_embedder_and_flag`
covers each missing piece, and `test_sqlite_never_reports_recall` covers the
case that would spawn indexing tasks writing to a table SQLite does not have.

`recall` also returns `[]` rather than raising when unavailable, so a caller
that skipped the capability check degrades instead of breaking.

**Fourth instance of the two-clock `created_ts`.** SQLite `time.time()`,
Postgres `DEFAULT now()`. Here it sets the TTL sweep boundary:
`delete_older_than` compares that column against an application-side cutoff, so
host clock skew moves retention by exactly that much.

**Fourth instance of positional row reads** — `PgArtifactStore.get` used
`row[0]`. Named access throughout.

39 parity tests, weighted toward the windowed-read maths (`offset`/`length`,
clamping, the `length=-1` whole-body read `grep` depends on): an off-by-one
there hands the agent the wrong region of a file with nothing failing.

Two stale class-name assertions found and replaced with behaviour checks —
`test_build_storage.py` asserted `type(...).__name__.startswith("Pg")`, which
cannot survive one store on two backends. It now asserts `supports_recall`,
which is what the name stood for.

**AI. Memory: the `getattr` probes are gone, and a two-clock tiebreaker with them.**

`SqliteMemoryStore` was the last store without `backfill_embeddings`, which is
why three call sites discovered it with
`getattr(store, "backfill_embeddings", None)`. `UnifiedMemoryStore` declares it
unconditionally — returning 0 where there are no vectors, which is the true
answer — so `cli/agent_stack.py`, `daemon/bootstrap.py` and `daemon/main.py` now
simply call it. **All three probes deleted**, closing the complaint
`test_twin_conformance.py` had recorded since Phase 2.

`KNOWN_ASYMMETRIES` is down to one entry (`ArtifactStore.recall`), and that one
was always the *good* pattern — a declared `supports_recall` property.

Guarded by `test_no_getattr_probes_remain_in_production`, which walks the AST of
every module under `yuyutsava/` rather than grepping lines: several modules now
*describe* the retired pattern in their docstrings, and a text match would flag
the documentation of the fix as the defect. Negative-controlled by reinserting
one probe — caught with file and line.

Two divergences fixed on the way:

* **`created_ts` came from two clocks** — SQLite `time.time()`, Postgres
  `DEFAULT now()`. The third domain in a row with this exact shape. It bites
  harder here than in transcripts or feedback because `created_ts DESC` is the
  **tiebreaker** in keyword ranking: two memories written moments apart could
  order differently depending on backend.
* **The kind filter used two different constructs** — `kind = ANY(%s)` on
  Postgres, `kind IN (?,?,?)` on SQLite. Not cosmetic: this clause is how
  session purge keeps `fact`/`preference` memories and drops the ephemeral
  kinds. One portable `IN` now, asserted on both.

Near-duplicate suppression stays Postgres-only and is asserted as a **declared**
difference — it compares cosine similarity, and faking it on SQLite would mean a
text-equality shortcut that behaves unlike the real thing.

35 parity tests.

> **Three-instance pattern, now named.** `created_ts` written by the app on
> SQLite and by the database on Postgres appeared in transcripts (AE), feedback
> (AH) and memory (AI). Every remaining domain with a timestamp column should be
> checked for it *before* migrating rather than discovered during.

**AH. Feedback: the record handed back was not the record stored.**

`FeedbackStore.upsert` builds a `MessageFeedback` with `created_ts=time.time()`
and **returns it to the caller**. The SQLite twin wrote that value. The Postgres
twin left `created_ts` out of its INSERT entirely and let `DEFAULT now()` fire —
the database server's clock.

So on Postgres the caller received a timestamp that was never persisted. Not a
drift that needs a sweeper to expose, like finding AE: the returned object and
the stored row disagreed *immediately*, and nothing raised. Anything that
rendered or logged the return value showed a time the database did not have.

Fixed by writing the column explicitly on both.
`test_returned_record_matches_what_was_stored` compares the two.

Re-rating also became a single `ON CONFLICT ... DO UPDATE` on the existing
`(thread_id, message_ref)` unique index, replacing DELETE-then-INSERT.

**Honest limit on that second change.** No test here fails if the store reverts
to DELETE-then-INSERT — I checked, by reverting it. Both statements ran inside
one `d.write()`, and a single connection cannot observe the gap between them.
The upsert is justified as removing a correctness dependence on the caller's
transaction (the Postgres twin ran on the autocommit path until Phase 2 fixed
it — one of the six pre-existing bugs), **not** by a red test. The suite's
docstring and the test name both say so rather than implying coverage that
does not exist.

What did get stronger: `test_rollback.py`'s guard was a source-grep for
`transaction()`. It now asserts the upsert issues **exactly one** statement and
contains no `DELETE`, which is a property that holds regardless of how any
caller wraps it.

Third domain running where a twin's row mapper indexed positionally
(`r[0]` … `r[9]`) — see findings AF and AG.

**AG. Tasks: a shared row-mapper that only worked by accident.**

`SqliteTaskStore` and `PgTaskStore` shared one `_row_to_record` built on
`tuple(row)`, with a comment claiming it "works for both aiosqlite.Row (mapping)
and psycopg tuples because the SELECT column order above is fixed."

It worked because `pool.connection()` yields **tuples**. The dialect's read
connection uses `dict_row`, and `tuple()` over a mapping yields its **keys** —
so the shared helper could not be carried into the unified store at all. The
same trap appeared in `PgVectorSearch` during the skills migration (finding AF).
Twice in two domains is a pattern, so it is written down: *anything reading rows
positionally is coupled to a row factory it does not name.* `UnifiedTaskStore`
maps by column name, which is backend-neutral and cannot be shifted by adding a
column.

Also generalised `Dialect.ensure_parent` to forward `**attrs`. The Postgres twin
called `ensure_thread(conn, rec.thread_id, origin=rec.origin)` on insert; the
dialect's signature had no way to carry `origin`, and dropping it would leave
every task-created `threads` row with a null origin — the task insert is often
what *creates* that row and is the only caller that knows where the work came
from. Pinned by `test_origin_is_recorded_on_the_hub_row`.

30 parity tests. Two properties get explicit coverage because they are quiet
when wrong: `task_id` is *both* the primary key and the pagination cursor (ULID,
so `ORDER BY task_id DESC` is chronological — a collation difference would skip
or repeat rows while paging rather than raise), and `update` interpolates its
column names, so `_check_fields` against `_MUTABLE_COLUMNS` is the injection
boundary and is now asserted on both backends instead of trusted.

**AF. Skills: the asymmetry was real, so it is now declared instead of probed.**

The first domain where the twins were **not** one query in two dialects.
Postgres ranked by pgvector cosine similarity; SQLite counted `LIKE` matches.
Different algorithms, different orderings — collapsing them into one SQL string
would mean dropping semantic search or pretending SQLite has it.

So retrieval stays two strategies. What changed is how callers find out:

```python
# before — three call sites, duck-typed
backfill = getattr(store, "backfill_embeddings", None)
if backfill is not None: ...

# after
await store.backfill_embeddings()      # always exists; returns 0 with no vectors
store.supports_semantic_search         # declared, for callers that report
```

`test_twin_conformance.py` had already named the `getattr` probe as the bad
pattern and `ArtifactStore.supports_recall` as the good one. `Dialect` now
carries `supports_vectors`, so the store never re-derives it.

**Not yet removed: the probes themselves.** They also cover `memory_store`,
which has not migrated, so deleting them today would `AttributeError` on SQLite.
Both sites are annotated with the dependency; they go when playbook order 12
lands. Recording this rather than claiming the probes are gone.

Two things the shared contract caught that the twins hid:

* **`(x LIKE ?) + (y LIKE ?)` is not portable.** SQLite treats a boolean as 0/1
  and adds it; Postgres refuses — *"operator does not exist: boolean + boolean"*.
  The Postgres twin carried a `::int` cast the SQLite one did not, so neither
  file could have run against the other's backend. Now standard `CASE WHEN`,
  no dialect hook needed.
* **`PgVectorSearch` builds `Hit`s by positional index** (`row[0]`, `row[1]`),
  so it cannot take the dialect's `dict_row` read connection. The vector path
  uses the pool directly — and the pool is *passed in*, not read off
  `dialect._pool`, so `Dialect` stays a Protocol with no private names in it.

30 parity tests. The agent scope filter is asserted on **both** search paths: a
degraded backend may rank worse, but must never return another agent's skills.

**AE. Transcripts: two clocks for one timestamp column.**

`transcript_messages.created_ts` was written by **different clocks depending on
the backend**. `SqliteTranscriptStore` passed `time.time()`; `PgTranscriptStore`
omitted the column and let `DEFAULT now()` fire — the *database server's* clock.

`UnifiedSweeper` then compares that column against an application-side cutoff
(`time.time() - ttl`). On SQLite both sides come from one clock; on Postgres they
come from two. Any skew between the app host and the database host shifts the
retention boundary by exactly that skew — invisibly, and in whichever direction
the drift runs. Small today (same machine), unbounded the moment Postgres is
remote, which is the deployment the Postgres backend exists for.

The unified store writes it explicitly on both, via `Dialect.ts_param()`.

Two lesser divergences went with it: the dedup keyword (`INSERT OR IGNORE` vs
`ON CONFLICT DO NOTHING` — both backends support the standard form, so that is
what is used), and `content` decoding (SQLite always parsed; Postgres parsed only
`if isinstance(content, str)` — same result, two different assumptions about the
driver, now stated once in `Dialect.json_value`).

This domain is also the proof that the dialect is the right size: it is the first
to need **every** capability at once — `json_param`/`json_value`,
`ts_param`/`epoch`, `ensure_parent`, `write` — and needed no new ones.
25 parity tests, both backends.

**AD. The events package is fully migrated — 7 domains, 0 hand-written twins.**

`EventStore`, `ProposalStore`, `DecisionStore`, `ConsentRuleStore`,
`ConsentGrantStore`, `ToolCounterStore` and `PendingAskStore` all run on one
implementation over the dialect adapter. `pg_stores.py` is down to
`PgPrefsBackend`; `sqlite_backend.py` to the backend itself and
`SqlitePrefsBackend`.

Measured, both backends live, 92 parity tests:

| Metric | Before | After |
|---|---:|---:|
| Twin pairs, whole codebase | 19 (start of Phase 2) | **9** |
| Twin pairs, events package | 7 | **0** |
| Events package, **code** lines | 821 | **764** |
| Events package, **raw** lines | 1101 | 1552 |

**Raw lines went up by 451, and that is the honest headline.** The unified
stores carry substantially more documentation than the twins did, and the
per-call dialect indirection (`d.ph(12)`, `d.write(_do)`) is more verbose than a
hardcoded `?`. Excluding docstrings and comments the package is 57 code lines
smaller — a rounding error either way.

The saving this buys is not line count. It is **2 → 1 implementations to edit
per change**, and the fact that the 92-test parity suite runs the *same*
contract against both backends, so a divergence cannot be introduced silently.
That is what the twins could not offer at any line count: finding AC is a
divergence that had been sitting in the schema, and it took writing the shared
contract to surface it.

The dialect itself (108 code lines) is a fixed cost already amortised across
visuals, summaries and voice.

Two things moved for correctness while migrating:

* **`events/ask_wire.py`** (new). The `pending_asks` column order and wire
  encoders lived in `sqlite_backend.py` — fine when only SQLite used them, wrong
  once the *unified* store had to import its own column order out of a
  backend-specific module. The wire format is backend-independent, so it now
  lives somewhere backend-independent.
* **`Dialect.json_param()` / `json_value()`** (new). Postgres stores
  `payload_json` as `jsonb` (needs `%s::jsonb` on write, returns a parsed
  `dict`); SQLite stores TEXT (returns a `str`). Callers index into the payload
  without checking, so the same row yielding two Python types is a live bug
  waiting on a backend switch. `test_payload_comes_back_as_a_dict_on_both_backends`
  pins it.

**AC. Postgres has two foreign keys SQLite does not, and one of them is retention.** ✅ **CLOSED 2026-08-09 — schema v5.**

Found by the parity suite refusing to insert a proposal on Postgres:

```
ForeignKeyViolation: insert or update on table "proposals"
violates foreign key constraint "proposals_event_fk"
```

Live-database inventory (`pg_constraint`) versus `events/schema.py`:

| Constraint | Postgres | SQLite |
|---|---|---|
| `proposals.event_id → event_payloads ON DELETE CASCADE` | present | **absent** |
| `decisions.proposal_id → proposals ON DELETE SET NULL` | present | **absent** |

The SQLite events schema contains **no `REFERENCES` clause at all**.

The consequence is not the insert path — every producer already writes the
payload first (`events/source.py:70`, `task_submission.py:100`). It is the
**sweep**. `UnifiedSweeper._sweep_events` deletes `event_payloads` older than
`event_ttl_sec` (7 days), and:

* on **Postgres** the cascade takes every `proposals` row for those events with
  it, and nulls the `decisions.proposal_id` that pointed at them;
* on **SQLite** nothing happens — the rows stay, now with dangling ids.

Neither `proposals` nor `decisions` has a TTL sweep of its own on either
backend (`domains.py:127-128` — both are `ROW_DELETE`, purged only by session
deletion). So Postgres garbage-collects them as a **side effect of a foreign
key**, and SQLite — the default, zero-config backend — **grows without bound**.

Not a data-loss bug: proposals expire after 300s while the sweep runs at 7 days,
so nothing still-actionable is destroyed. It was an unbounded-growth divergence,
and *schema*-level, so unifying the store code could not fix it.

**Resolved by adding the constraints to SQLite** (user decision, 2026-08-09).
Both now match Postgres exactly. Three things made it more than a schema edit,
each negative-controlled:

**1. Enforcement.** SQLite defaults `PRAGMA foreign_keys` to **OFF**, and it was
set nowhere in the codebase. A `REFERENCES` clause without it parses, stores and
does nothing — a *worse* state than no constraint, because the schema then
claims an invariant it does not hold. `SqliteEventsBackend.open` now enables it
after migrating and outside any transaction (the pragma is a silent no-op inside
one). Control: pragma removed → 4 tests fail.

**2. The migration.** SQLite has no `ALTER TABLE ADD CONSTRAINT`, so v5 rebuilds
both tables. Existing databases can already hold rows that violate the new
constraint — orphan proposals whose event was swept, which is the drift itself.
`proposals.event_id` is `NOT NULL` so those are dropped (exactly what Postgres
did via cascade); `decisions.proposal_id` is nullable so those are nulled, since
the decision is the audit record and must survive. Gated per-table on
`PRAGMA foreign_key_list`, not on the version anchor: `schema_meta` is written at
the *end* of `migrate`, so a fresh DB reports `current = 0` and a version-only
gate would rebuild two correct tables on every first boot.

**3. The spillover buffer — the hazard the FK introduced.** In Postgres mode
these tables are a write buffer. The reconciler drains parents first (Postgres
needs the parent row before the child) and deletes each drained batch as it
goes — so with cascade live, deleting a drained `event_payloads` row takes that
event's **not-yet-drained proposals** with it and they never reach Postgres.
Silent, and only during outage recovery. `Reconciler.reconcile` now runs inside
`SqliteEventsBackend.foreign_keys_off()`. Control: suspension removed → the
buffered proposal never reaches Postgres, caught by name.

Production needed no change: both bus paths (`events/source.py:70`,
`task_submission.py:154`) already persist the payload before publishing, so no
producer can create an orphan. Two *test fixtures* did, and now seed the parent.

18 tests in `test/storage/test_events_foreign_keys.py`.

**A test of mine that tested nothing.** `FreshDatabaseIsNotRebuilt` originally
asserted only that no scratch tables were left behind — which a *redundant*
rebuild also satisfies, since it tidies up after itself. It passed with the gate
deleted. Rewritten around `sqlite_master.rootpage`, which moves when a table is
dropped and recreated, plus a control proving the probe can detect a rebuild at
all. The same exercise showed an outer "both FKs present → return" fast path was
a second gate no control could distinguish from its own absence, so it was
removed: one gate, demonstrably load-bearing.

**AB. The unbound-name guard produced a false positive, and that is a defect.**

Extracting the async-subagent block (152 lines — the largest in `build_daemon`)
made `test_bootstrap_no_unbound_names.py` report two names. One was real:

```python
subagent_settings: LLMSettings,   # the class is LlmSettings
```

An annotation-only error, invisible at runtime under
`from __future__ import annotations`, and caught for free.

The other was the guard's own fault. It collected parameters from
`FunctionDef`/`AsyncFunctionDef`/`ClassDef` but **not `ast.Lambda`**, so
`middleware_factory=lambda sa: ...` reported `sa` as unbound. It had passed
before only by luck: `build_daemon` bound `sa` elsewhere, and moving the lambda
into a function that did not took the luck away.

Worth writing down because a guard that cries wolf is worse than no guard — it
teaches you to override it, and the next report is a real finding you skip. Both
directions are now controlled:

| Control | Result |
|---------|--------|
| Reintroduce finding Y's shape (import moved, use left behind) | **caught**, named + line number |
| Remove the `ast.Lambda` fix | **false-positives again** on `sa` |

Also converted five `None`-able names into one bundle with a named invariant.
`build_daemon` gated three call sites on `async_host_url is not None` rather
than on `async_host` — because a process that *attached* to a host another
process owns has a URL but no host object, and can still submit runs. That
distinction lived in a comment; it is now `AsyncSubagentSubsystem.available`,
with `test_attached_process_is_available_without_a_host_object` pinning it.
Getting it backwards silently disables background delegation for every attached
process — no error, just nothing ever delegated.

**AA. The globals are retired one call site at a time, not all at once.**

`F-S08` names 7 `set_default_*` globals. Measured surface: **91 `get_default_*`
call sites**. A big-bang removal is one large change with no incremental
verification — precisely the shape that produced finding Y — so 3.4 is
**additive**:

```python
purge_session(session_id, *, ctx: AppContext | None = None)
```

`AppContext` is a frozen dataclass of optional store handles. Each resolver
returns the explicit handle if given, else the global. Pass one and the
dependency is explicit and test-isolated; omit it and behaviour is byte-for-byte
what it was. Call sites migrate independently.

`purge_session` went first because it is the case the review names: a
**one-argument signature that touches four stores**, so no caller could see its
blast radius, and no test could isolate it without setting and restoring four
globals.

Two things this bought immediately, beyond the honesty:

1. **A stronger test.** `test_feedback_purge.py` asserted the wiring by
   *grepping `purge_session`'s source* for `get_default_feedback_store` — the
   only observation available at the time. A grep passes on a call sitting in
   dead code. It now runs a real purge against a real session and watches the
   store get called.
2. **A duplicate store removed.** The events store is *constructed*, not global,
   so its resolver returns `(store, caller_owns_lifecycle)`. The daemon keeps one
   open on `app.state.store` for the whole process, yet
   `DELETE /sessions/{id}` was opening and closing a **second** one per request.
   The endpoint now hands its own over; unwired callers still get a per-call
   store. Returning the ownership flag is what makes that safe — otherwise the
   callee has to guess whether it may `stop()` a handle someone else is using.

Proven by `test/storage/test_app_context.py` (8 tests), whose flagship case runs
the **full purge with the globals booby-trapped**: `_Landmine` stores raise if
consulted, so any fallback fails the test naming the offending store. Negative
controls, all confirmed failing when the seam is broken:

| Control | Result |
|---------|--------|
| Resolver ignores the explicit handle | **2 failures** |
| `purge_session` calls the global directly | **2 failures** |
| Landmines not armed (`test_landmines_are_armed`) | asserts the decoys really do raise |

**Not done:** 90 call sites still resolve from globals, and `GLOBAL_CONTEXT`
exists precisely to keep them working. This is a seam, not a removal.

**Z. The Postgres-only branch is now covered by execution, not just analysis.**

Finding Y's bug survived because the block lived inside ``build_daemon``, which
starts uvicorn, the LangGraph host and MCP servers and therefore never returns
in a test. Nothing could reach it. The static unbound-name guard caught that
*instance*; it would not catch a logic error in the same branch.

``build_retrieval`` extracts it. The Postgres-only path — building
``TodoNoteIndex`` and installing it via ``set_default_note_index`` — is now a
59-line function with explicit inputs, and ``test/daemon/test_build_retrieval.py``
runs it against the live server.

Negative-controlled by reintroducing finding Y's exact bug:

| Suite | With the bug present |
|-------|----------------------|
| `test_build_storage.py` (SQLite) | **still green** — as it was originally |
| `test_build_retrieval.py` (Postgres) | **NameError, 2 failures** |

That is the coverage gap closed. The SQLite case is not filler either: it
asserts the note index is correctly **absent** without pgvector, so the
exchange's embed-on-write hooks no-op — an omission there would silently break
`todo_recall` rather than raise.

The general principle, now demonstrated twice: **extracting a branch-guarded
block from an unrunnable function is what makes it testable.** Splitting
``build_daemon`` is not only a readability change; each slice converts a
previously unreachable path into one a suite can execute.

**Y. An extraction broke Postgres boot, and every test suite stayed green.**

Moving the retention block into `build_retention` took
`from yuyutsava.todoboard.exchange import get_default_exchange` with it — but
`build_daemon` still called `get_default_exchange()` further down, in the
TODO-note-recall block. A `NameError`, introduced by a mechanical extraction.

**Nothing caught it.** That block is guarded by
`if pg_pool is not None and embedder is not None`, so it only runs on Postgres;
every SQLite suite passed, the framework contracts passed, and all 353 modules
imported cleanly. An import error is loud — an *unbound name inside a branch you
do not exercise* is silent until that branch runs, on the backend you happen not
to be testing.

A static unbound-name pass found it in 6 ms. It is now
`test/daemon/test_bootstrap_no_unbound_names.py`, negative-controlled: re-break
it and it names the exact line and symbol.

Two lessons, the second more general than the first:

1. **Extractions strand imports.** Moving a block moves its imports; uses left
   behind become unbound. Mechanical, easy to repeat, invisible to import checks.
2. **"All suites green" is a statement about executed paths.** This is the
   fourth time in this project that a green result covered less than it appeared
   to — after the unwired context stores, the unwired async subagents, and the
   orchestrator's missing `search_config`. Branch-guarded code needs a check
   that does not depend on reaching the branch.

Also worth recording: my first attempt to verify the split was to call
`build_daemon` in a script. It timed out at 9m20s — not a hang, but a badly
designed probe: `build_daemon` starts uvicorn, the LangGraph host and MCP
servers, so it is *not meant to return*. **Full-daemon boot remains unverified
by automation**; the three extracted builders are each verified standalone.

**X. The cycles are real and measurable — 9 of them.**
Before assuming the review was right, the package graph was rebuilt from
**top-level imports only** (deferred ones hide the coupling): 9 package-level
cycles, with `core` in five —
`agents↔core`, `agents↔daemon`, `agents↔cli`, `core↔storage`, `core↔llm`,
`core↔skills`, `core↔tools`, `async_subagents↔daemon`, `channels↔daemon`.

That is why ~10 dependency fields were `object | None` with the real type in a
comment: **there was no acyclic direction to declare them in.**

`ports/` is a leaf both sides can import. Existing stores satisfy the protocols
**structurally** — verified via `isinstance` — so nothing inherits anything and
no call site changed. Only declared types moved.

Three fields remain `object | None`, each for a stated reason rather than
inertia:

| Field | Why it stays |
|-------|--------------|
| `remote_async_subagents`, `async_task_mirror` | Genuine optional dependency — typing them pulls `langgraph_api` in when background subagents are off. Not a cycle workaround |
| `context_settings` | A frozen config dataclass, not a behavioural seam; a Protocol over plain values would be noise |

One useful negative result: `prefs.runtime` has **zero** internal imports, so it
is in no cycle and needed no port for that reason. It got one anyway — consumers
only call `subagents()`, and a one-method protocol says so where the concrete
class exposes far more. Worth separating "breaks a cycle" from "narrows a
surface"; only the first was load-bearing.

The leaf guard has three checks, and the third is the one that matters: it
imports `ports` in a **fresh interpreter** and asserts no other `yuyutsava`
module was pulled in. A static scan cannot catch a cycle created by a package
`__init__` re-export; that can.

## Phase 4 — Framework boundary 🚧 IN PROGRESS

**Go decision taken 2026-08-10** by the project owner, after ADR-004's
recommendation to default to *no*. Recorded plainly: the ADR's argument against
was cost (4 weeks, high risk, no user-visible benefit), not doubt about the
design. Of ADR-004's four justifications, the one that holds here is **item 3 —
testability**: the 14 policy classes are effectively untested because testing
them means constructing framework objects, and this project already avoids heavy
import tests for that reason. That is a present tax, not a contingency, so it is
what the phase is being justified on and measured against.

| Step | Task | Status | Notes |
|------|------|--------|-------|
| 4.1 | `ModelHandle` (ADR-004 item 2) | **`DONE`** | Provider declares name + capabilities. **Found a live bug: every Azure usage row recorded a blank model name.** Findings AV, AW |
| 4.2 | `yuyutsava/policy/` — our Policy contract + one adapter | **`DONE`** (tool hooks) | `LangChainPolicyAdapter` reproduces middleware nesting; 21-test contract suite. Model-call hooks deferred to 4.6 — see below |
| 4.3 | `AskUser` port (ADR-004 item 3) | **`DONE`** | `ports/ask.py` + `LangGraphAskUser` / `ScriptedAskUser`. `interrupt()` is out of the permission path |
| 4.4 | Migrate the 14 policies | **`DONE` 14 / 14** | Every policy is a plain object. `AgentMiddleware` subclasses **14 → 1**. Findings AX–BH |
| 4.5 | `Agent` protocol + one driver loop | **`DONE`** (`F-D03`) / **partial** (`F-T03`) | One `_drive_graph` + two sinks; **found and fixed a live drift**. `F-T03`'s *constructing* half is untouched. Findings BJ, BK |
| 4.6 | Model-call hooks on the adapter | **`DONE`** | `ModelCall` records edits; the adapter replays them. The 4-times-duplicated prompt-append block now exists once |
| 4.7 | Collapse the per-policy adapters into one | **`DONE`** | `cli:wired` **12 middleware entries → 2**. Per-chain order byte-identical across all 9 configs. Finding BI |
| 4.8 | Observer hooks (`before/after_model`, `after_agent`) | **`DONE`** | `Turn`/`Usage`/`Directive`; usage extraction de-duplicated out of budget + usage recorder |

### Baseline, measured before any Phase 4 change

| Metric | Phase 4 start | Now |
|--------|---------------|-----|
| `AgentMiddleware` subclasses (written as such) | 14 | **1** — the adapter |
| `AgentMiddleware` subclasses (incl. inherited) | 15 | **2** — plus `YuyutsavaCompactionMiddleware`, which extends the framework's own `SummarizationMiddleware` |
| Migrated `Policy` classes | 0 | **14** |
| Modules importing a framework (AST) | 69 / 381 (18%) | **60 / 382 (15%)** |
| Modules importing a framework (ADR's `grep -rl`) | 93 | 99 ⚠️ **the metric is unusable — finding AY** |
| Framework contracts | 8/8 | 8/8 |
| Fingerprint gate | 10/10, 9 configs | 10/10, 9 configs |
| Modules importing cleanly | 374 | 381 |

### Why 4.2 covers tool hooks only

Reading the fourteen policies before designing the contract showed four distinct
hook shapes, not the two ADR-004 sketched: request **revision** before a model
call (5 policies), pure **observation** after one (2), observation that may
**append messages** (`BudgetMiddleware` injects a wrap-up `SystemMessage`), and
the **tool** hooks (6). One `on_tool_call(ctx) -> PolicyAction` cannot express
that set.

Tool hooks went first because they contain the hardest policy, which is what
ADR-004's risk table asks for: *"migrate the most demanding policy first —
`PermissionMiddleware` … if the adapter handles that one, it handles the rest."*
It refuses outright, asks the user, and does both conditionally in a fixed order,
so it exercised every part of the contract at once.

### Findings

**AV. Every Azure usage row has been recording a blank model name.**

Not a fragility argument — a measured defect, found by writing down what
`model_name_of` returns per provider *before* changing it:

```
azure: {"name": "", "cls": "AzureChatOpenAI"}      ← every other provider: a real name
```

`model_name_of` probed `model_name` → `model` → `model_id` on the built object.
`AzureChatOpenAI` is constructed from `azure_deployment` and never given
`model`, so it leaves `model_name` at `None` and all three probes miss. The
model column on Azure usage rows — the input to cost attribution — has been
empty, and nothing failed.

This is precisely the failure ADR-004 item 2 predicted: *"deletes
`model_name_of`'s six-way duck-typing by having the provider return what it
already knows"*. The provider knows the name from settings, at build time,
before an SDK object exists to interrogate.

The fix is in `Provider.model_name()`'s **default** (read `settings.model`), not
in the Azure override — confirmed by negative control: removing the override
still names the model `gpt-4o`, and only the empty-`model` case regresses. The
override covers that remaining hole, where the deployment name is the only true
identifier.

**AW. My own registry leaked every model it recorded — caught by its own test.**

`model_name_of` needs to answer for a model held long after its settings are
gone, so the built model's identity is recorded at the one construction seam.
The first version stored the whole `ModelHandle` in that registry — and a
`ModelHandle` holds `.model`. The registry kept the handle, the handle kept the
model, so the `weakref.finalize` that evicts the entry could never fire.

25 models built, `del`, full `gc.collect()` → **25 still registered**. A daemon
that builds a fresh model per task would have grown that dict without bound.

Fixed by storing a model-free `_Identity` and rebuilding the handle on demand.
The leak test was written alongside the registry rather than after it, which is
the only reason this was a five-minute fix instead of a slow memory problem in
production.

**AX. Approving a protected-directory delete prompts you a second time.**

Found by the parity matrix, not by looking for it. `rm -rf .venv` matches the
scope check (protected subdirectory) *and* the pattern check (recursive
deletion), and approving the first does not skip the second — so the user is
asked twice about one command, with two different reasons.

Both implementations do this identically, which is how it surfaced: the first
matrix run scripted one answer and the *new* side ran out of answers while the
old side silently answered twice. Pre-existing behaviour, so the suite **pins it
rather than changing it** — `venv delete second denied` exists precisely to keep
the second prompt from being optimised away by accident. Whether it *should*
prompt twice is a product question, recorded here rather than decided inside a
refactor.

**AY. ADR-004's own tracking metric is unusable as a gate.**

The ADR proposes measuring framework coupling as:

```
grep -rl "langchain\|langgraph\|deepagents" yuyutsava | wc -l    # target: <25
```

It counts files that *mention* the words. Measured during this step: the number
went **93 → 99 while the first policy was being migrated off the framework** —
every point of the rise came from docstrings in the new framework-free modules
explaining what they avoid and why.

Replaced with `scripts/measure_framework_surface.py`, which parses imports:

```
modules scanned            381
import a framework          69 (18%)
AgentMiddleware subclasses  14
migrated Policy classes      1
```

A metric that punishes documenting the boundary is worse than no metric, because
it argues against the thing it is supposed to encourage.

**Also worth stating plainly:** the subclass count has not moved yet (13
originals + the adapter). One policy of thirteen is migrated. The count only
falls as 4.4 proceeds, and reporting it as progress before then would be
counting the plan rather than the work.

**BA. One sibling guarded a crash; the other did not, and nobody noticed.**

``BackgroundTaskCapMiddleware`` and ``AsyncTaskInterruptPatchMiddleware`` both
gated on ``request.tool.name``. ``request.tool`` is ``None`` whenever the model
names a tool that is not bound — a hallucination or a typo. The cap guarded that
explicitly, with a comment saying why:

```python
# request.tool is None when the model's tool call didn't resolve to a
# bound tool (hallucinated/mistyped name) — let handler() run the
# normal unknown-tool path instead of crashing on `.name`.
```

The interrupt-patch middleware, two files away, did not. Measured:

```
interrupt_patch: AttributeError: 'NoneType' object has no attribute 'name'
cap            : ok -> 'RAN'
```

So a single mistyped tool name killed the whole turn on any master with async
subagents, where the framework would otherwise have reported an unknown tool and
let the model recover.

**The migration removes the class of bug, not just this instance.** A policy
reads ``ToolCall.resolved_tool``, a ``str | None`` the adapter derives once, so
``call.resolved_tool == "..."`` is the only thing you *can* write. This is the
one deliberate behaviour change in the tool-policy migrations, and
``test_interrupt_patch_parity.py`` records both halves of it.

**BB. A `ports/` Protocol declared two methods `async` that are not.**

Mine, from Phase 3 step 3.2. ``TaskMirror`` declared:

```python
async def count_running(self) -> int: ...
async def list_non_terminal(self) -> list: ...
```

``AsyncTaskMirror`` implements both synchronously — they read an in-memory dict —
and every caller calls them plainly. Nothing caught it because
``runtime_checkable`` ``isinstance`` compares method **names** only: no
signatures, no return types, no coroutine check. The mirror satisfied a contract
it did not implement, and anyone trusting the annotation would have written
``await mirror.count_running()`` and got ``TypeError: object int can't be used in
'await' expression``.

Fixed, and ``ProtocolsAgreeWithTheirImplementations`` now compares async-ness for
wired protocol/implementation pairs — negative-controlled by re-declaring one
``async`` and watching it go red.

**BC. Two ordering rules went silently vacuous the moment I renamed a class.**

The fingerprint gate enforces middleware ordering by name:

```python
if earlier not in stack or later not in stack:
    continue  # not both wired in this configuration
```

That skip is correct when a configuration genuinely lacks one of the two. It is
catastrophic when a name merely *changed*. Renaming ``ToolResultOffloadMiddleware``
to ``ToolResultOffloadPolicy`` silently disabled **two of the four rules** —
including "suppressed tools must be filtered before their results are offloaded"
— and the suite stayed green.

Two fixes. Rules now match against a new ``order`` fingerprint field (behaviours
in run order, adapters expanded) so repackaging cannot break them. And
``test_every_order_rule_actually_fires`` asserts each rule fires in at least one
configuration: **a rule that fires nowhere is not a rule.** Negative-controlled by
reinstating the stale name — it goes red naming the exact rule.

This is the third time in this review that a green suite was covering less than
it appeared to (after the dead tripwire in Phase 0 and the vacuous seam-3 test).
The pattern is always the same: a check keyed on a name, and the name moved.

**BD. The framework forbids two adapters in one stack — found by building a graph.**

``create_deep_agent`` validates:

```python
if len({m.name for m in middleware}) != len(middleware):
    raise AssertionError("Please remove duplicate middleware instances.")
```

``AgentMiddleware.name`` defaults to the class name, so **every agent stopped
building** the moment a fourth policy got its own adapter. The plan of one
adapter per policy — chosen precisely so each policy stays at the position its
middleware held — is not something the framework allows by default.

**The fingerprint gate could not have caught this.** It intercepts
``create_deep_agent`` and records the kwargs, so the validation never runs there;
all 12 fingerprint tests were green while no agent could be built.
``scripts/verify_framework_contract.py`` caught it, because it is the one check
that actually builds a graph. That is the entire argument for keeping a slow,
real-construction test alongside the fast structural ones.

Fixed by giving the adapter a ``name`` derived from its policies
(``LangChainPolicyAdapter[ToolFilterPolicy]``), which satisfies uniqueness and
required **no ordering change at all** — collapsing the adapters is now a
separate, deliberate step (4.7) rather than something forced by a build error.

**BE. "Nothing drives a graph synchronously" was true only where I looked.**

The adapter raises on the sync path rather than silently skipping every policy,
justified by a check that scanned ``yuyutsava/`` and found no ``.invoke()``. The
check passed. The claim was still wrong: ``test/test_filesystem_prompt_override.py``
— the Phase 0 tripwire that renders the real system prompt — called
``bundle.agent.invoke()``, and step 4.6 broke it.

Two things follow. The tripwire was **rendering the prompt through hooks
production never runs**, which is a weaker test than it looked; it now uses
``ainvoke``. And the premise check was stated about "the codebase" but scoped to
production code — the same mistake as finding AU, where a check covered only the
shape I had happened to edit. It now scans ``test/`` and ``scripts/`` too, and
excludes itself (it contains the pattern it greps for, so without that it reports
itself forever).

**BF. Three no-system-message branches, three different answers.**

``VoiceStyleMiddleware`` stripped leading newlines from its addendum
(``addendum.lstrip("\n")``), ``SubagentGateMiddleware`` used the addendum
verbatim, and ``RetrievalInjectionMiddleware`` dropped the ``"\n\n"`` separator it
adds in every other case. Same question, three answers, none recorded as a
decision.

Surfaced because ``ModelCall`` has to answer it *once*, in the adapter. The
retrieval case was the only real divergence across **9 policies × 7 request
shapes**, and it is reproduced via ``ModelCall.has_system_prompt`` rather than
harmonised — deepagents always supplies a system prompt, so none of this runs in
production, and quietly changing prompt text inside a migration is not a trade
worth making.

**BG. The metric flattered the work until I checked it.**

``scripts/measure_framework_surface.py`` matched the literal base name in
``class X(AgentMiddleware)`` and reported **1 remaining subclass**.
``YuyutsavaCompactionMiddleware`` extends ``SummarizationMiddleware``, which
extends ``AgentMiddleware`` — a framework subclass the count missed, because it
inherits one level deeper.

The number that would have gone in the report was therefore wrong in the
flattering direction. The scanner now resolves it by import and ``issubclass``
and prints **both**: the AST count, which is what ADR-004's "one adapter" target
is about, and the exact total. ADR-004's verification — *"exactly one
``AgentMiddleware`` subclass remains"* — is met for the policy layer; compaction
is deliberately not a policy, it is a framework middleware we subclass to
customise, and it is still there.

**BH. Two token-extraction routines, one spend ceiling.**

``BudgetMiddleware._accumulate`` and ``UsageRecorder._tokens`` each did the same
thing: find the last ``AIMessage``, read ``usage_metadata``, cope with it being a
dict on some providers and an object on others, coerce to ``int``. Two copies,
and what they extract is the input to a spend cap and to every cost row.

Resolved once by the adapter into ``Turn.usage``. The policies now read a typed
value, and the "no usage reported" case — which both already treated as *skip*,
never as zero — has one definition instead of two. A zero row is
indistinguishable from a genuinely free call once costs are summed, so that
distinction is worth having in one place.

**BI. Three of the four "middleware ordering rules" were not about ordering.**

The gate enforced four rules by list position. Mapping every stack entry to the
hook chain it actually participates in:

| entry | chain |
|---|---|
| ToolFilter / FilesystemPrompt / VoiceStyle / SubagentGate / RetrievalInjection | `model_call` |
| SubagentGate / Permission | `wrap_tool_call` (before) |
| ToolResultOffload | `wrap_tool_call` (after) |
| YuyutsavaCompaction / TranscriptRecorder / PromptInspector | `before_model` |
| TranscriptRecorder / Budget / Usage | `after_model` |

Only same-chain pairs are positional. Three of the four rules paired entries in
**different** chains:

* *"offload before compaction"* — a tool hook against a before-model hook. Tool
  results are written to state during tool execution, which is always before the
  next model call, whatever order the two sit in.
* *"compaction before budget"* — `before_model` always precedes `after_model`
  within a turn.
* *"tool filter before offload"* — different chains again.

Those orderings are **true**, and they would have stayed true however the list
was arranged. So the rules passed for three phases while enforcing a property
the list does not control — the same "reads as coverage, isn't" shape as findings
BC and AU.

Rewritten as per-chain rules against a new ``chains`` fingerprint field, plus
``CrossChainFactsAreNotListOrder``, which pins the thing that *is* worth
checking: that each pair really is still in different chains. If one ever moved
chains, the loop-structure argument stops holding and a real rule is needed.

**This had to be fixed before 4.7 could be judged at all.** The collapse changes
list positions wholesale; against the old rules that looks like a violation, and
against the corrected ones it is provably a no-op.

**Measured, per ADR-004's risk table** (*"the overhead should be negligible, but
'should be' is not a measurement"*), 5 real policies, median of 2000 calls:

| | bare | collapsed | per-policy | cost of the layer |
|---|---|---|---|---|
| tool call | 0.25µs | 1.00µs | 4.25µs | **+0.75µs** |
| model call | 6.92µs | 19.67µs | 34.75µs | **+12.75µs** |

Dispatch only — the work *inside* a policy is unchanged code. Against a model
turn of hundreds of milliseconds, 13µs is roughly 0.01%. The collapse also
removes ~3µs/tool-call and ~15µs/model-call of nesting.

**BJ. The resume protocol was written twice and the fix landed in one copy.**

`F-D03` says *"the resume protocol is hand-implemented twice, in two 226-line
functions"*. Here is the bill.

LangGraph needs `Command(resume={id: answer})` once more than one interrupt is
pending. When none of them carries an id, that map cannot be built. The daemon
driver falls back to a scalar resume with the first answer, with a comment
explaining why:

```python
elif all(it_id is None for it_id, _ in decisions):
    # ... rather than dropping every answer and effectively rejecting.
    current_input = Command(resume=decisions[0][1])
```

The CLI driver had no such branch. It built an empty map and resumed with
`Command(resume={})` — **discarding every answer the user had just typed**, on
the surface where a human is sitting there answering.

Measured on a scripted graph: `resumed: ['{}']` before, `['approve']` after.
Both drivers now call one `_resume_command`.

**Honest accounting**, same discipline as Phase 2's line finding:

| | before | after |
|---|---|---|
| raw lines | 818 | 844 (+26) |
| **code lines** (AST, docstrings excluded) | **599** | **564 (−35)** |
| the loop | written twice (230 + 226 lines) | `_drive_graph`, 92 lines, shared |

The file got *longer* and the code got shorter — the added lines are the
docstrings explaining the driver/sink split and why the resume protocol has one
home now.

**BK. Two of four negative controls did not fire, and the matrix was the reason.**

After the merge, four deliberate breakages were run against the parity suite.
Two changed nothing observable:

* *never close the AI stream on an interrupt* — no scenario streamed tokens
  straight into an interrupt;
* *never fire `on_tick`* — no scenario passed an `on_tick` hook at all, so the
  session runner's progress signal was entirely uncovered.

A third, *stop closing the stream when a tool call starts*, still did not fire
after a scenario was added for it: with only a trailing update afterwards the
newline lands either way. It needed a text chunk **after** the tool-call chunk
to become observable.

Three scenarios added, and — because `git HEAD` still held the genuine pre-merge
driver — the golden for all 13 was re-captured from **that**, not from the new
code. A golden recorded from the implementation it is meant to check is not
evidence. All four controls now fire.

**BL. The cost ledger reads $0.00 for the model actually in use.**

Found by running the daemon and reading its own usage endpoint.
``estimate_cost_usd`` prefix-matches the price table and returns ``0.0`` for
anything unmatched — documented, but silent. The table ships
``gemini-2.5-flash``; the configured model is ``gemini-3.5-flash``, which
prefix-matches **nothing**:

```
gemini-3.5-flash      matches=NONE                 cost(48390in,159out)=$0.000000
gemini-2.5-flash      matches=['gemini-2.5-flash'] cost(48390in,159out)=$0.014915
```

Every call on the running configuration is booked as free. The usage view shows
a number that is not a number, and nothing anywhere says so.

**Not fixed by inventing a price** — we genuinely do not know it, and a guessed
number in a cost ledger is worse than an admitted gap. Fixed the Phase 0 way:
warn once per unknown model, naming it and the file to add it to. The value
stays ``0.0``; the silence goes.

**BM. Triage's `drop` outcome was recorded nowhere at all.**

``TriageLoop._handle`` writes a decision row and a timeline line for every
outcome — ``skipped_by_rule``, ``auto_approved``, ``logged`` — except one:

```python
if decision.action == "drop":
    return
```

Consequence for a ``mode=triage`` submission: the registry row stays ``queued``
(deliberate v1, per ``submit_via_triage``), and with no decision written there is
**no evidence anywhere that triage ever saw it**. "Triage judged this not worth
acting on" and "the daemon is broken" are the same observation from the UI.

Found by submitting two triage tasks and watching them sit queued with an empty
decisions feed. Fixed by recording ``outcome="dropped"`` with the classifier's
reason plus a timeline line, matching every sibling branch.

**Left alone deliberately:** the task row still stays ``queued``. That is
documented v1 behaviour and changing it is a product call, not a bug fix. Worth
deciding separately — a dropped task arguably belongs in a terminal state.

**BN. A restart executed proposals the user had declined. (Consent bypass.)**

The user started the daemon and it immediately ran two tasks nobody asked for.
They were mine — `mode=triage` submissions left over from UI testing — and the
mechanism is the serious part:

```
resume: re-enqueued 2 interrupted task(s) from previous run
```

Both rows: `status=queued`, `proposal=pending`, `decision=expired`. Triage had
proposed them, the user never answered, the proposals **timed out**. On the next
boot `resume_interrupted_tasks` re-enqueued every non-terminal row and the
orchestrator ran them — one attempting to write to the user's TODO board.

That contradicts the system's own second invariant: *nothing acts without a
standing rule or an explicit approval.* **A restart was a way around Tier-1
consent.**

`queued` conflates two states the schema cannot separate, because `TaskRecord`
has no `proposal_id`:

| submitted via | when it is `queued` |
|---|---|
| `submit_direct` | the instant between writing an **approved** proposal and enqueueing |
| `submit_via_triage` | its whole life, waiting on a proposal — and after it is skipped, dropped, or expires |

Fixed by resuming **`running` only**, with the skipped rows logged. The asymmetry
decides it: being conservative costs a re-submission of something that never
started; being permissive runs what the user declined.

**I got this wrong the day before.** After fixing the silent-`drop` bug (BM) I
wrote that leaving the row `queued` was "deliberate v1 behaviour… a product
decision, not a bug fix." That was wrong, because I never connected it to
`resume_interrupted_tasks`. Queued-forever is harmless; queued-until-the-next-restart
is a consent bypass. The lesson is narrow and repeatable: *a state left "harmlessly"
wrong is only harmless until something else reads it.*

**BO. `find_duplicate` broke every memory write on Postgres.**

From the same daemon session:

```
memory: task_outcome write failed
  File ".../retrieval/pg.py", line 143, in find_duplicate
    if row and float(row[1]) >= threshold:
KeyError: 1
```

Postgres rows arrive in two shapes: a pooled connection yields **tuples**, a
connection inside `Dialect.write()`/`reading()` yields **mappings**.
`UnifiedMemoryStore.add` deliberately runs the dedup probe inside the insert's
transaction — so `find_duplicate` got a mapping and read positionally.

This is finding AT's mirror image. AT was *the dialect leaking `dict_row` to
other consumers*; this is *the dialect handing `dict_row` to a collaborator that
reads positionally*. Fixing the leak did not fix the collaborator.

**Why 35 parity assertions missed it:** the dedup probe is guarded by
`self._dedup_threshold <= 1.0`, and `pg_memory_store` defaults it to `1.1` —
dedup **off**. Only the daemon passes the configured `0.97`. A default that
disables the feature meant the suite exercised the store thoroughly and this
branch not at all. The new suite passes the daemon's value, and asserts
*deduplication actually happens* — otherwise a `find_duplicate` that always
returned `None` would pass too.

**BP. A structural check on a document proves nothing about what it renders.**

After updating both architecture docs I verified: fences balanced, section
numbering contiguous, every TOC anchor correct, every referenced file and
identifier present, every numeric claim matching the source. All green.

The user then pointed out that **diagram 15 was not appearing**.

The mindmap label I had written — `AskUser: HITL without interrupt()` — is a
parse error, because Mermaid reads `word(...)` as node-*shape* syntax and the
empty parens leave it with no description:

```
Parse error on line 41: ...L without interrupt()
Expecting 'NODE_DESCR', got 'NODE_DEND'
```

The failure mode is the bad kind: the Markdown is valid, the fence is balanced,
the file reads correctly in an editor — and the diagram is simply absent when
rendered. **No check on the document could see it. Only rendering can.**

`scripts/verify_diagrams.py` now renders every Mermaid block through the local
Kroki service (already in `docker-compose.yml`, so nothing leaves the machine),
and additionally flags mindmap labels containing shape characters *before*
rendering, because Kroki's error points at a line inside the block rather than
in the file. It skips loudly when Kroki is down rather than reporting a pass.

Same lesson as findings BD and BK, in a third medium: **a check that inspects
the artifact is not a check that exercises it.** BD needed a graph actually
built; BK needed the driver actually run; this needed the diagram actually
drawn.

**AZ. Three unrelated things are now called "policy" — flagged, not fixed.**

| Name | What it is |
|------|-----------|
| `yuyutsava/core/policy.py` → `PermissionsPolicy` | **user config** from `~/.yuyutsava/permissions.json` — which tools skip the prompt |
| `yuyutsava/core/agent_profiles.py` → `Policy` (enum) | **a declaration** of which cross-cutting concerns a master attaches |
| `yuyutsava/policy/` → `Policy` (base class) | **the contract** a cross-cutting concern implements |

Nothing is broken — the module paths are distinct and Python resolves them
unambiguously — but a reader hitting `Policy` has to work out which of three is
meant. The enum is the odd one out (it declares *which*, not *what*), so
`PolicyKind` would be the honest rename.

Not done here: renaming it touches the profiles, the fingerprint gate and the
divergence registry, and doing that inside the same change as a behavioural
migration would blur the diff that proves the migration was safe. Recorded as a
carry-over.

---

## Verification log

Commands and their actual results. No entry added on the basis of expected behavior.

| Date | What was verified | Command | Result |
|------|-------------------|---------|--------|
| 08-08 | Baseline: existing tripwire | `python test/test_filesystem_prompt_override.py` | ❌ `AttributeError: no attribute 'chat_model'` — dead |
| 08-08 | Repaired tripwire | same | ✅ block stripped, invariant holds |
| 08-08 | **Negative control, seam 1** | disable `_is_fs_block`, rerun | ✅ went RED |
| 08-08 | New contract tripwires | `python test/framework_contract/test_deepagents_contract.py` | ✅ 4/4 green |
| 08-08 | **Negative control, seams 2–4** | break each contract, rerun | ✅ 4/4 went RED (after fixing the vacuous seam-3 test) |
| 08-08 | Ceilings admit installed versions | specifier check over `pyproject.toml` | ✅ 62 packages checked; caught + fixed my own bad `<3` on `langgraph-checkpoint-postgres` (installed 3.1.0) |
| 08-08 | Warn-on-silent-failure | healthy vs. reworded-block simulation | ✅ silent when healthy; exactly 1 warning when broken |
| 08-08 | Public re-exports identical | `pub.X is types.X` for 5 symbols | ✅ all identical — import move is a runtime no-op |
| 08-08 | Import normalization safe | import all 15 middleware modules | ✅ all import; 0 `.types` sites remain |
| 08-08 | No package-wide regression | walk + import every module | ✅ 340 modules, 0 errors |
| 08-08 | Touched-area tests | 6 standalone suites | ✅ all pass |
| 08-08 | Pre-existing failure isolated | stash change, rerun | ✅ fails identically without my change → not a regression |
| 08-08 | Offload test repaired | `python test/context/test_offload_middleware.py` | ✅ 6 pass (was 5 pass + 1 fail) |
| 08-08 | **Phase 0 exit gate** | `python scripts/verify_framework_contract.py` | ✅ 7/7 contracts hold, exit 0 |
| 08-08 | C1: backend deprecation cleared | run any agent build, count warnings | ✅ 0 deepagents-0.7 deprecation warnings (was 2+ per build) |
| 08-08 | C2: CLI entry point intact | `python -m yuyutsava.cli.cli --help` | ✅ runs; `build_agent` gone from `yuyutsava.core` |
| 08-08 | Capability matrix conformance | `python test/core/test_agent_profiles.py` | ✅ 6 tests; caught one transcription error in my own matrix (orchestrator MCP via `mcp_manager`, not `mcp_tools`) |
| 08-08 | **Negative control, fingerprint gate** | scramble order / drop middleware / remove tools | ✅ 18 failures / detected / named — gate proven before use |
| 08-08 | **1.2a equivalence** | `python test/core/test_agent_fingerprint.py` | ✅ snapshot byte-identical across all 6 configurations after extraction |
| 08-08 | Injector chain precision | fingerprint digest of `RetrievalInjectionMiddleware` | ✅ order + SkillInjector scope asserted per profile |
| 08-08 | Post-refactor regression | 341-module import sweep + 5 standalone suites | ✅ no import errors; all suites pass |
| 08-08 | Framework contracts after Phase 1 work | `python scripts/verify_framework_contract.py` | ✅ 8/8 hold |
| 08-08 | Intent classification | `python yuyutsava/core/agent_profiles.py` | ✅ 12 divergences → 8 by-design, 4 unreviewed |
| 08-08 | 1.2b async extraction | 9-config fingerprint gate | ✅ async path now executed (3 async middlewares, opt-in filter, remote peer) |
| 08-08 | **Whole-phase equivalence** | `git stash` engine.py, regenerate all 9, diff | ✅ only diff is `backend: function → LocalShellBackend` (the intended C1 fix) |
| 08-08 | Phase 1 regression sweep | contracts + profiles + fingerprint + 341-module import | ✅ 8/8, 7/7, 7/7, 0 errors |
| 08-08 | 4 divergence decisions applied | 9-config fingerprint diff vs snapshot | ✅ only the 4 intended changes; 0 tool/prompt/subagent drift |
| 08-08 | Divergence ratchets | `python test/core/test_agent_profiles.py` | ✅ total 12→8, **unreviewed 4→0** |
| 08-08 | Post-decision regression | contracts + profiles + fingerprint + 341 modules | ✅ 8/8, 7/7, 7/7, 0 import errors |
| 08-08 | 1.2c context-tools extraction | 9-config fingerprint diff | ✅ all 9 identical |
| 08-08 | 1.3 profile-driven tool families | same | ✅ identical; profile is now prescriptive |
| 08-08 | Harness gap #3 (orchestrator ws_*/sk_*) | supply search_config + skill_registry | ✅ orchestrator 11 → 22 tools |
| 08-08 | Routing boundary asserted directly | `ProfileDrivenToolFamilies` | ✅ orchestrator binds no tr_/db_/vis_ |
| 08-08 | **Phase 1 acceptance** | `python test/core/test_fourth_master_cost.py` | ✅ 4th master = 21 lines of data; helpers proven role-agnostic |
| 08-08 | Phase 1 final regression | contracts + 3 suites + 341 modules | ✅ 8/8, 5/5, 10/10, 5/5, 0 errors |
| 08-08 | Twin structural conformance built | `python test/storage/test_twin_conformance.py` | ✅ 8 tests over 19 twin pairs; found 3 asymmetries (all verified guarded) |
| 08-08 | **Atomicity audit** | AST scan for multi-write under autocommit | ⚠️ 16 non-atomic PG methods found (SQLite twins atomic) |
| 08-08 | Atomicity fix | convert to `_pool.transaction()` | ✅ 16 fixed, 0 remaining; ratchet added |
| 08-08 | **Purge completeness audit** | compare purge lists vs created tables | ⚠️ `message_feedback` never purged; holds verbatim user/assistant text |
| 08-08 | Purge fix | `delete_for_thread` on both twins + wired into `purge_session` | ✅ `python test/storage/test_feedback_purge.py` 4/4 |
| 08-08 | Post-Phase-2-start regression | 7 suites + contracts + 341 modules | ✅ all pass, 0 import errors |
| 08-08 | **Atomicity count corrected** | AST parse vs keyword regex | ⚠️ regex said 16, truth is **8**; ratchet switched to the AST counter |
| 08-08 | SQLite explicit rollback | added to `_run_write` (+ `BaseException` for cancellation) | ✅ hardening — negative control shows tests pass without it too |
| 08-08 | SqliteEventsBackend.transaction() | new multi-statement helper | ✅ rollback + commit verified |
| 08-08 | **PG rollback proven on live server** | 2-write method raising mid-way, PG 16.14 | ✅ `connection()` → 2 rows survive; `transaction()` → 0. Fix is load-bearing |
| 08-08 | Both-backend rollback suite | `python test/storage/test_rollback.py` | ✅ 9/9 (SQLite + events backend + live PG) |
| 08-08 | Post-transactional regression | 12 suites + contracts + 341 modules | ✅ all pass |
| 08-08 | Live PG schema introspection | 34 tables, 19 thread/session-scoped | ⚠️ purge covered 11; found `pending_asks` unaccounted |
| 08-08 | Domain registry derivation | derived lists vs hand-maintained | ✅ byte-identical (8 SQLite, 11 PG) before adding anything |
| 08-08 | `pending_asks` purge fix | `delete_for_thread` on both twins + Store facade + purge | ✅ end-to-end: thread1 purged, thread2 intact, no text leaked |
| 08-08 | **Registry completeness negative control** | create an undeclared table, re-check | ✅ flagged `_undeclared_probe` — the check is not blind |
| 08-08 | Post-registry regression | 16 suites + contracts + 342 modules | ✅ all pass |
| 08-08 | Dialect adapter built | `yuyutsava/storage/dialect.py` | ✅ 5 backend differences normalised; rows are mappings on both |
| 08-08 | **Visuals collapsed to one implementation** | `UnifiedVisualStore` | ✅ 211 → 95 lines (54% less) |
| 08-08 | **Parity across 4 implementations** | `python test/storage/test_visual_store_parity.py` | ✅ 40/40 — SqliteTwin, SqliteUnified, PostgresTwin, PostgresUnified |
| 08-08 | Post-dialect regression | 13 suites + contracts + 344 modules | ✅ all pass |
| 08-08 | Visuals cutover | `bootstrap.py` + `get_default_visual_store` | ✅ both paths run `UnifiedVisualStore`; end-to-end save/get/delete verified |
| 08-08 | Twins deleted | 225 lines removed from `visuals/store.py` | ✅ 14 suites still green; twin ratchet 19 → 18 |
| 08-08 | **Honest line accounting** | AST count, docstrings excluded | ⚠️ raw lines went UP (359→391); real saving is **-50 code lines/domain** |
| 08-08 | Summary store migrated | `summary_store_unified.py` + parity suite | ✅ 28 assertions across 4 impls; **found a live PG race** |
| 08-08 | PG version-allocation race | `test_concurrent_puts_get_distinct_versions` | ⚠️ `PgThreadSummaryStore` FAILS, `UnifiedThreadSummaryStore` PASSES |
| 08-08 | Summary twins deleted + cut over | 3 call sites, 110 lines removed | ✅ twin ratchet 18 → 17 |
| 08-08 | Post-migration regression | all suites + contracts + 345 modules | ✅ 0 failing |
| 08-08 | Voice store migrated | `voice_store_unified.py` + parity suite | ✅ 52 assertions across 4 impls, both live backends |
| 08-08 | Voice twins deleted + cut over | 178 lines removed | ✅ twin ratchet 17 → 16 |
| 08-08 | Post-migration regression | all suites + contracts + 346 modules | ✅ 0 real failures (2 web suites need pytest, pre-existing) |
| 08-08 | UsageStore sized for migration | read both twins | ⚠️ PAUSED — found ad-hoc dialect helpers + a user-visible task_id divergence (finding U) |
| 08-08 | Migration playbook written | `07-migration-playbook.md` | ✅ procedure + remaining 15 domains in order |
| 08-08 | **Finding U resolved — Option C** | `group_by=thread` added to store + endpoint + schema | ✅ per-card cost identical on both backends: `todo:card_42 -> $0.05` |
| 08-08 | Option C regression | `python test/storage/test_usage_thread_grouping.py` | ✅ 11/11; storage divergence deliberately pinned as unchanged |
| 08-08 | Post-Option-C sweep | all suites + contracts + 346 modules | ✅ 0 failing |
| 08-08 | **2.6 StoreFactory** | `storage/factory.py` + `Failover` policy in `domains.py` | ✅ store-selection branches 13 → 0 |
| 08-08 | Factory equivalence | `python test/storage/test_store_factory.py` | ✅ 8/8 both backends; spillover set pinned to exactly 3 domains |
| 08-08 | Live factory probe | build all 10 stores on both backends | ✅ SQLite → all sqlite/unified, Postgres → Pg + 3 RoutedStore |
| 08-08 | Post-2.6 regression | all suites + contracts + 347 modules | ✅ 0 failing |
| 08-08 | events batch sized | read all 7 pairs | ⚠️ found 3 idempotency divergences (6 flagged, 4 false positives) |
| 08-08 | **Idempotency fix** | 3 SQLite puts given `ON CONFLICT DO NOTHING` | ✅ `test_events_idempotent_put.py` 5/5 + a parity ratchet |
| 08-08 | Post-fix regression | all suites + contracts + 347 modules | ✅ 0 failing |
| 08-08 | `EventsSqliteDialect` added | wraps the persistent-connection `SqliteEventsBackend` | ✅ unblocks all 7 events domains |
| 08-08 | events domains 1–2 migrated | `events/unified.py` (tool counters, consent rules) | ✅ 44 parity assertions across 4 impls, both live backends |
| 08-08 | events twins deleted + cut over | `Store` facade uses the unified pair on both backends | ✅ **twin ratchet 16 → 14** |
| 08-08 | Post-events regression | all suites + contracts + 348 modules | ✅ 0 failing |
| 08-08 | **2.7 Store roles** | `storage/events/roles.py`, 12 structural Protocols | ✅ `Store` satisfies all 12 with no inheritance, no call-site changes |
| 08-08 | 7 consumer signatures narrowed | prefs, consent, triage, task_submission, orch_loop, sweeper, spawn | ✅ `python test/storage/test_store_roles.py` 5/5 |
| 08-08 | Post-2.7 regression | all suites + contracts + 349 modules | ✅ 0 failing |
| 08-08 | Cycle measurement | package graph from top-level imports | ⚠️ **9 real package-level cycles**, `core` in 5 |
| 08-08 | **3.1 ports/ extracted** | 9 protocols, zero internal imports | ✅ `python test/test_ports_is_a_leaf.py` 3/3 incl. fresh-interpreter check |
| 08-08 | 3.2 dependency fields typed | `OrchestratorDeps` 11 → 3 `object \| None` | ✅ structural conformance verified by `isinstance` |
| 08-08 | Post-3.1 regression | all suites + contracts + 353 modules | ✅ 0 failing (2 pre-existing, see below) |
| 08-08 | ⚠️ **Billable call made unintentionally** | `test/test_async.py` in a bulk sweep | 2 Vertex agent turns, ~8k in / 600 out tokens (extrapolated). Guard added — now requires `YUYUTSAVA_ALLOW_BILLABLE=1` |
| 08-08 | **3.3 build_storage extracted** | `StorageSubsystem` record + `build_storage(opts)` | ✅ builds standalone on BOTH backends in <1s |
| 08-08 | build_daemon shrinkage | AST measure | 927 → **803** lines, 58 → **40** branches |
| 08-08 | Standalone-buildability proof | `python test/daemon/test_build_storage.py` | ✅ 5/5 — was impossible before (needed a whole daemon) |
| 08-08 | Post-3.3 regression | all suites + contracts + 353 modules | ✅ 0 failing |
| 08-08 | build_policy + build_retention extracted | `bootstrap.py` | ✅ `build_daemon` 803 → **749** lines, 40 → **37** branches |
| 08-08 | ⚠️ **Extraction broke Postgres boot** | static unbound-name pass | `get_default_exchange` stranded — NameError on the PG-only path; ALL suites stayed green |
| 08-08 | Guard added + negative-controlled | `test/daemon/test_bootstrap_no_unbound_names.py` | ✅ names the exact line/symbol when re-broken |
| 08-08 | Post-3.3 regression | all suites + contracts | ✅ 0 failing |
| 08-08 | `build_retrieval` extracted | 4th slice; contains the PG-only note-index branch | ✅ `build_daemon` 749 → **720** lines, 37 → **33** branches |
| 08-08 | **PG-only branch now executed** | `python test/daemon/test_build_retrieval.py` | ✅ 4/4 on both backends |
| 08-08 | **Negative control vs finding Y** | reintroduce the stranded import | ✅ SQLite suite stays green; **PG suite fails with NameError** |
| 08-08 | Post-3.3 regression | all suites + contracts | ✅ 0 failing |
| 08-08 | `build_events` extracted | 5th slice: bus + sources + SIGHUP reload hook | ✅ `build_daemon` 720 → **711** lines, 33 → **31** branches |
| 08-08 | Post-slice regression | all suites + contracts + unbound-name guard | ✅ 0 failing |
| 08-10 | **Phase 4 go decision** | recorded in PROGRESS | ✅ justified on ADR-004 item 3 (testability), not portability |
| 08-10 | Phase 4 baseline | count subclasses + framework importers | ✅ 14 AgentMiddleware subclasses, 93 framework-importing modules |
| 08-10 | Pre-change name behaviour recorded | build all 11 provider configs, call `model_name_of` | ⚠️ 10 return a real name; **azure returns `""`** (finding AV) |
| 08-10 | `AzureChatOpenAI` probed directly | read `model_name`/`model`/`model_id` | ✅ `None`/`None`/absent — confirms the blank is the SDK, not the call site |
| 08-10 | 4.1 `ModelHandle` built | `yuyutsava/llm/handle.py` + `Provider.model_name`/`capabilities` | ✅ framework-free at runtime (annotation deferred) |
| 08-10 | **Name equivalence** | rebuild all 11 configs, diff vs recorded | ✅ 10 byte-identical; azure `""` → `gpt-4o` (the only intended change) |
| 08-10 | **Negative control, Azure** | delete `AzureOpenAIProvider.model_name`, rerun | ✅ went RED — but only the empty-`model` case, proving the base default is what fixes it |
| 08-10 | **Registry leak caught** | `TheRegistryDoesNotLeak` | ⚠️ 25 built → 25 still registered after gc (finding AW); fixed, now 0 |
| 08-10 | 4.1 suite | `python test/llm/test_model_handle.py` | ✅ 24/24 |
| 08-10 | 4.1 regression | contracts + fingerprint + profiles + loop-affinity | ✅ 8/8, 10/10, 8/8, 19/19 |
| 08-10 | 4.1 import sweep | walk + import every module | ✅ 374 modules, 0 errors |
| 08-10 | 4.1 full sweep | every standalone suite | ✅ 0 new failures (4 pre-existing: embedder, uncommitted `chat_repl.py`, 2 needing pytest) |
| 08-10 | 4.2 policy core built | `yuyutsava/policy/` (types, base, adapter, ask) | ✅ one AgentMiddleware subclass in the package, ratcheted |
| 08-10 | Adapter contract | `python test/policy/test_adapter.py` | ✅ 21/21 — nesting order, first-refusal-wins, escape hatch, dead-hook skipping |
| 08-10 | 4.3 AskUser port | `ports/ask.py` + LangGraph/Scripted impls | ✅ `interrupt()` no longer called from the permission path |
| 08-10 | **Parity BEFORE cutover** | `python test/policy/test_permission_parity.py` | ✅ old middleware vs new policy over a 19-case matrix, both live |
| 08-10 | ⚠️ Matrix assumption wrong twice | first parity run | `/usr/local/lib` is not system-critical; `rm -rf .venv` prompts TWICE (finding AX) |
| 08-10 | **Negative control ×3** | reword refusal / disable scope check / accept any answer | ✅ 3, 8 and 8 cases went RED, each naming the case |
| 08-10 | Cutover diff proven exact | old snapshot vs new, field by field | ✅ **6 permission swaps, 0 unexpected differences** across all 9 configs |
| 08-10 | Fingerprint taught about containers | `_middleware_digest` + flattened `policies` key | ✅ stack reports `LangChainPolicyAdapter[PermissionPolicy]`, not a bare class name |
| 08-10 | Golden record captured | `test/policy/permission_golden.json` | ✅ 25 cases, paths normalised `<WS>`/`<CRITICAL>` so it holds on Windows too |
| 08-10 | Old class deleted | `permission_middleware.py` 341 → 252 lines | ✅ zero framework imports left in it; rules were always framework-free |
| 08-10 | Divergence note re-keyed | `python yuyutsava/core/agent_profiles.py` | ✅ 8 by-design, **unreviewed still 0** |
| 08-10 | **Wired-build verification** | adapter pulled out of a real `build_cli_deepagent` | ✅ 5 cases incl. workspace-root threading and a live refusal |
| 08-10 | **Negative control, wiring** | drop workspace_root / attach nothing | ✅ 2 and 5 cases went RED — the AT/AU blind spot is covered here |
| 08-10 | 4.4 regression | contracts + fingerprint + profiles + ports-leaf | ✅ 8/8, 10/10, 8/8, 6/6 |
| 08-10 | 4.4 import sweep | walk + import every module | ✅ 381 modules, 0 errors |
| 08-10 | 4.4 full sweep | every standalone suite | ✅ 0 new failures (same 4 pre-existing) |
| 08-10 | ⚠️ ADR-004's grep metric | `grep -rl` before vs after | went **93 → 99 while coupling fell** — replaced (finding AY) |
| 08-10 | ⚠️ **Sibling-guard bug found** | drive both middlewares with `request.tool=None` | `interrupt_patch` raises AttributeError, `cap` returns fine (finding BA) |
| 08-10 | ⚠️ **`ports/` Protocol wrong** | compare TaskMirror decls vs AsyncTaskMirror | 2 of 3 methods declared `async`, implemented sync (finding BB) |
| 08-10 | Protocol agreement guard | `python test/test_ports_is_a_leaf.py` | ✅ 8/8; negative control (re-declare async) goes RED naming the method |
| 08-10 | `ToolCall` extended | `resolved_tool`, `Denied.status`/`.named` | ✅ existing policy suites still green (21/21, 19/19) |
| 08-10 | ⚠️ **Sync tool path would silently skip every policy** | grep for `.invoke()`/`.stream()` on any graph | none exist; `wrap_tool_call` now RAISES rather than omitting the hook |
| 08-10 | Cap policy parity | `python test/policy/test_cap_parity.py` | ✅ 11/11 incl. the `>=` boundary and unresolved-tool guard |
| 08-10 | Offload policy parity | `python test/policy/test_offload_parity.py` | ✅ 10/10; stored artifact compared with `length=-1`, not the default slice |
| 08-10 | Check-guard parity | `python test/policy/test_check_guard_parity.py` | ✅ 15/15; `Raw` used by exactly one policy, ratcheted |
| 08-10 | Interrupt-patch parity | `python test/policy/test_interrupt_patch_parity.py` | ✅ 9/9 incl. both halves of finding BA |
| 08-10 | **Cutover diff proven exact** | old snapshot vs new, incl. subagent stacks | ✅ **17 in-place swaps, 0 unexpected differences** |
| 08-10 | ⚠️ Subagent digest was blind | first diff attempt | reported a bare `LangChainPolicyAdapter`; `_subagent_digest` now uses `_middleware_digest` |
| 08-10 | ⚠️ **2 ordering rules went vacuous** | renamed class, rules matched by name | silently stopped asserting (finding BC) |
| 08-10 | Ordering fixed + guarded | `order` fingerprint field + `test_every_order_rule_actually_fires` | ✅ 11/11; negative control on the stale name goes RED |
| 08-10 | Golden records captured | `test/policy/tool_policies_golden.json` | ✅ 31 cases across 4 middlewares, before deletion |
| 08-10 | 4 middleware classes deleted | 3 modules removed, 1 trimmed to `RemoteThreadPatcher` | ✅ **AgentMiddleware subclasses 14 → 10** |
| 08-10 | 4.4 import sweep | walk + import every module | ✅ 382 modules, 0 errors |
| 08-10 | 4.4 full sweep | every standalone suite | ✅ 0 new failures (same 4 pre-existing) |
| 08-10 | Framework surface | `python scripts/measure_framework_surface.py` | 67/382 import a framework (17%); 10 subclasses; **5 migrated policies** |
| 08-10 | 4.6 `ModelCall` + adapter model hooks | `yuyutsava/policy/{types,base,adapter}.py` | ✅ the 4-times-duplicated prompt-append block now exists once |
| 08-10 | **Model-call parity BEFORE cutover** | `python test/policy/test_model_call_parity.py` | ✅ 9 policies × 7 request shapes vs their middlewares |
| 08-10 | ⚠️ **One divergence, exactly where predicted** | `NoSystemMessage` | retrieval dropped a `\n\n` (finding BF); fixed via `has_system_prompt` |
| 08-10 | **Negative control ×3** | drop separator / reword addendum / drop a suppressed prefix | ✅ 4, 7 and 2 cases went RED |
| 08-10 | ⚠️ **UnboundLocalError on every CLI build** | first fingerprint run after cutover | a leftover LOCAL import shadowed the new module-level one |
| 08-10 | Shadowed-import guard added | `test/daemon/test_bootstrap_no_unbound_names.py` | ✅ 3/3; control names the exact line; now covers `engine.py` too |
| 08-10 | ⚠️ **Duplicate middleware rejected by the framework** | `python scripts/verify_framework_contract.py` | 2/8 BROKEN — no agent could build (finding BD) |
| 08-10 | Adapter `name` made unique | `LangChainPolicyAdapter[<policies>]` | ✅ 8/8 contracts; **zero ordering change needed** |
| 08-10 | ⚠️ **Sync graph path WAS reachable** | contract run after the fix | the Phase 0 tripwire used `agent.invoke()` (finding BE) |
| 08-10 | Tripwire driven the way production is | `asyncio.run(agent.ainvoke(...))` | ✅ and the premise check now scans `test/` + `scripts/`, self-excluded |
| 08-10 | ⚠️ **2 fingerprint checks went vacuous again** | injector-chain marker + skill-injector scope | `startswith` on the old class name returned `""`, which compared equal |
| 08-10 | Marker guard added | `test_the_marker_still_matches_something` | ✅ empty digest is now a failure, not a pass |
| 08-10 | **Cutover diff proven exact** | old snapshot vs new, incl. subagent stacks | ✅ **45 in-place swaps, 0 unexpected**; injector chain preserved inside the adapter |
| 08-10 | Golden records captured | `test/policy/model_call_golden.json` | ✅ 9 policies × 7 shapes, before deletion |
| 08-10 | 5 middleware classes deleted | 5 modules removed | ✅ **AgentMiddleware subclasses 10 → 5** |
| 08-10 | 4.6 import sweep | walk + import every module | ✅ 382 modules, 0 errors |
| 08-10 | 4.6 full sweep | every standalone suite | ✅ 0 new failures (same 4 pre-existing) |
| 08-10 | Framework surface | `python scripts/measure_framework_surface.py` | 64/382 (16%); **5 subclasses, 10 migrated policies** |
| 08-10 | 4.8 observer hooks | `Turn` / `Usage` / `Directive` + 3 adapter hooks | ✅ token extraction de-duplicated (finding BH) |
| 08-10 | **Observer parity** | `python test/policy/test_observer_parity.py` | ✅ 29/29 — transcript phases, budget boundary + verbatim directive, usage rows, inspector |
| 08-10 | **Negative control ×4** | cap `>=`→`>` / reword directive / drop seen-set / record zero-token calls | ✅ 1, 1, 2, 1 cases went RED |
| 08-10 | **Cutover diff proven exact** | old snapshot vs new, incl. subagent stacks | ✅ **32 in-place swaps, 0 unexpected** |
| 08-10 | Last 4 middleware classes deleted | 2 modules removed, 2 converted in place | ✅ **AgentMiddleware subclasses 5 → 1** |
| 08-10 | ⚠️ **The metric was flattering** | `issubclass` vs AST base-name match | compaction inherits one level deeper and was uncounted (finding BG) |
| 08-10 | Scanner corrected | prints AST count AND exact `issubclass` total | ✅ 1 written-as-such, 2 including inherited |
| 08-10 | 4.8 import sweep | walk + import every module | ✅ 382 modules, 0 errors |
| 08-10 | 4.8 full sweep | every standalone suite | ✅ 0 new failures (same 4 pre-existing) |
| 08-10 | **ADR-004 verification #1** | "exactly one AgentMiddleware subclass" | ✅ met for the policy layer; compaction is a deliberate framework subclass |
| 08-10 | **ADR-004 verification #2** | "every policy testable with no framework import" | ✅ 14/14 policies; parity suites construct `ToolCall`/`ModelCall`/`Turn` directly |
| 08-10 | **Hook-chain map built** | resolve every stack entry's chains from a real build | 6 chains; 3 of 4 ordering rules pair DIFFERENT chains (finding BI) |
| 08-10 | Ordering rules rewritten per chain | `chains` fingerprint field + `CrossChainFactsAreNotListOrder` | ✅ 13/13 green BEFORE any collapse |
| 08-10 | Per-chain baseline captured | 9 configurations | ✅ recorded pre-collapse |
| 08-10 | **4.7 collapse** | `collapse_policy_adapters()` at all 6 assembly sites | ✅ `cli:wired` 12 entries → 2; `cli:bare` 6 → 1 |
| 08-10 | **Collapse proven a no-op** | per-chain order, before vs after | ✅ **0 differences across 9 configs**; no non-middleware field moved |
| 08-10 | ⚠️ Wired test selected a policy positionally | `test_permission_parity.py` | index 0 stopped being PermissionPolicy; now selected by name |
| 08-10 | **Adapter overhead measured** | `python scripts/benchmark_policy_adapter.py` | +0.75µs/tool call, +12.75µs/model call; collapse saves ~3µs and ~15µs |
| 08-10 | 4.7 gates | fingerprint + contracts + profiles + fourth-master | ✅ 13/13, 8/8, 8/8, 5/5 |
| 08-10 | 4.7 full sweep | every standalone suite + 382-module import | ✅ 0 new failures, 0 import errors |
| 08-10 | 4.5 driver golden captured | 13 scenarios from `git HEAD`'s pre-merge driver | ✅ stderr + log records + return + resume Commands |
| 08-10 | ⚠️ **Resume-protocol drift found** | compare both drivers' multi-interrupt branches | CLI built `Command(resume={})`, dropping every answer (finding BJ) |
| 08-10 | **Drivers merged** | `_drive_graph` + `_run_config` + `_resume_command` | ✅ loop written once; two sinks |
| 08-10 | **Merge proven behaviour-preserving** | 13 scenarios vs golden | ✅ **12 byte-identical**; the 13th is the intended fix |
| 08-10 | Honest line accounting | AST, docstrings excluded | raw 818→844 (+26), **code 599→564 (−35)** |
| 08-10 | ⚠️ **2 of 4 negative controls did not fire** | break stream-close / on_tick | matrix could not see either (finding BK) |
| 08-10 | ⚠️ A 3rd control still silent after a fix attempt | tool-call-chunk guard | needed a text chunk AFTER it to be observable |
| 08-10 | Matrix widened, golden re-captured from HEAD | 10 → 13 scenarios | ✅ all 4 controls now fire (1, 12, 1, 2 failures) |
| 08-10 | `Agent` protocol | `yuyutsava/ports/agent.py`, 2 methods | ✅ `CompiledStateGraph` and the scripted double both satisfy it |
| 08-10 | `CompiledStateGraph` gone from the driver | grep | ✅ 0 references; ratcheted |
| 08-10 | **F-T03 scope stated honestly** | `test_the_constructing_half_of_F_T03_is_still_open` | `create_deep_agent` is still the only constructor — driving half only |
| 08-10 | 4.5 regression | driver parity + resume + routing + runner-crash + tasks API | ✅ 11/11, 7/7, all OK |
| 08-10 | 4.5 full sweep | every suite + 383-module import + contracts | ✅ 0 new failures, 0 import errors, 8/8 |
| 08-10 | **LIVE DAEMON TEST** — booted 10:09 on Phase 4 code | 12-point plan against the running daemon + Electron app | see below |
| 08-10 | Finding AT regression (pooled-connection KeyError) | GET/PATCH `/v1/settings/runtime` | ✅ prefs load and write clean — does NOT recur |
| 08-10 | Finding AU regression (artifacts NameError) | every attachment/bundle route + 4 error paths | ✅ 200s with right content-types; 404s not 500s |
| 08-10 | Agent invocation end-to-end | POST `/v1/tasks` → done | ✅ merged driver + collapsed adapter + 14 policies, real answer |
| 08-10 | Driver event vocabulary reaches the UI | `/v1/tasks/{id}/events` | ✅ log, tool_call, tool_result, token, timeline |
| 08-10 | SubagentGatePolicy live | ask orchestrator to use a disabled subagent | ✅ model read the prompt addendum and routed around it |
| 08-10 | HITL consent loop live | 3 asks → approve via API → file deleted | ✅ sequential interrupts resumed correctly (the 4.5 protocol) |
| 08-10 | Background subagent live | start_async_task → PONG | ✅ cap/interrupt-patch/check-guard on the live async host |
| 08-10 | TODO board round-trip | card+note+objective+attachment, then deleted | ✅ 10 user cards untouched |
| 08-10 | ⚠️ **BUG: cost ledger reads $0.00 for the live model** | `estimate_cost_usd('gemini-3.5-flash')` | prefix-matches nothing; ~$0.0149 of spend recorded as free (finding BL) |
| 08-10 | Fix: warn-once per unpriced model | `_warn_unpriced_once` | ✅ 1 warning per model, silent for priced ones |
| 08-10 | ⚠️ **BUG: triage `drop` was completely silent** | submit `mode=triage`, watch nothing happen | no decision row, no timeline, task queued forever (finding BM) |
| 08-10 | Fix + test | `python test/daemon/test_triage_drop_is_recorded.py` | ✅ 6/6; negative control (restore the silent return) → 3 red |
| 08-10 | Post-fix regression | full sweep + 383 modules + contracts | ✅ 0 new failures, 0 import errors, 8/8 |
| 08-10 | ⚠️ **CONSENT BYPASS on restart** | user's daemon ran 2 tasks unprompted after boot | `queued` rows with `proposal=pending`/`decision=expired` were resurrected and executed (finding BN) |
| 08-10 | Fix + test | `python test/daemon/test_resume_consent_boundary.py` | ✅ 7/7; only `running` resumes; control (re-add queued) goes RED naming the bypass |
| 08-10 | Old resume expectation corrected | `test/daemon/test_resume.py` | it asserted the buggy behaviour; now asserts the boundary |
| 08-10 | ⚠️ **`KeyError: 1` killing every memory write** | `find_duplicate` read `row[1]` on a `dict_row` connection | dialect hands mappings inside `write()`; helper read positionally (finding BO) |
| 08-10 | Fix + test | `python test/retrieval/test_find_duplicate_row_shape.py` | ✅ 8/8 both row shapes + real write path; control reproduces `KeyError: 1` |
| 08-10 | Post-fix regression | full sweep + 383 modules + contracts | ✅ 0 new failures, 8/8 |
| 08-10 | ⚠️ **A doc diagram silently stopped rendering** | user spotted diagram 15 missing | `interrupt()` in a mindmap label — `word()` is shape syntax (finding BP) |
| 08-10 | Fix + checker | `python scripts/verify_diagrams.py` | ✅ 34 diagrams across both docs render; control reproduces the exact parse error, exit 1 |
| 08-10 | ⚠️ **Doc audit: Phases 0 and 1 were NOT documented** | keyword audit of Architecture.md per phase | several "✅"s were one line in a file tree, not documentation |
| 08-10 | Gaps filled | new §7.0 capability matrix · §13.5 ceilings+tripwires · §6.1b declared dependencies | ✅ 5/5 phases now covered, 0 concepts missing |
| 08-10 | New claims verified against source | deepagents pin · 8/0 divergences · 24 fields 0 untyped · 21 Depends | ✅ all true |
| 08-10 | Docs re-verified | structure + files + identifiers + 34 diagrams | ✅ 29/29 TOC, 0 missing refs, all render |

