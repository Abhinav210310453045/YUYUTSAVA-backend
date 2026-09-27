# 10 — Legacy ledger

The status of every finding, carry-over and decision from the structural review
of 2026-08-08, formerly `docs/architecture/review/`, retired at W8: its ADRs are
in [`docs/architecture/decisions/`](../../architecture/decisions/README.md) and its
migration playbook is [`storage-migration-playbook.md`](../../architecture/storage-migration-playbook.md). That review's execution tracker
lives on the `yuyutsava-daemon` branch as `docs/internal/PROGRESS.md`.

Source docstrings that cite an old ID (`F-T04`, `ADR-004`, …) point here once the
old directory is retired. Each ID has its own heading, so a link such as
`10-legacy-ledger.md#f-t04` lands on it.

**How statuses were decided.**
- **Closed:** the old tracker marks the work done **and** a check passes at
  `f4ec45c` — a test run in this review, or a measurement.
- **Carried:** the problem is still measurable, so it has a new ID in this review.
- **Partly closed:** both are true.

Guard tests run for this ledger, all green at `f4ec45c`:

| Guard | Result |
|---|---|
| `test/core/test_agent_fingerprint.py` | 13 OK |
| `test/core/test_fourth_master_cost.py` | 5 OK |
| `test/core/test_driver_parity.py` | 11 OK |
| `test/storage/test_twin_conformance.py` | 9 OK |
| `test/test_ports_is_a_leaf.py` | 8 OK |
| `scripts/verify_framework_contract.py` | 8/8 |

## Summary

**Final (W8, 2026-09-28).** After the remediation:

| Status | Findings |
|---|---|
| **Closed** | F-S01, F-S02, F-S03, F-S04, F-S07, F-S08, F-S09, F-S10, F-S11, F-D01, F-D03, F-D06, F-K01, F-K02, F-K03, F-K04, F-T01, F-T02, F-T04, F-T05, F-T07, C1, C2 |
| **Partly closed** | F-S05, F-S06, F-S12, F-D02, F-D05, F-T03, F-T06 |
| **Carried** | F-D04, C3 |

Each ID below keeps its status at `f4ec45c` and adds its status at W8.

At `f4ec45c`:

| Status | Findings |
|---|---|
| **Closed** | F-S02, F-S03, F-S04, F-S07, F-S10, F-D03, F-T01, F-T02, F-T04, F-T05, F-T07, C1, C2 |
| **Partly closed** | F-S01, F-S05, F-S08, F-S12, F-D01, F-D02, F-D06, F-K04, F-T03, F-T06 |
| **Carried** | F-S06, F-S09, F-S11, F-D04, F-D05, F-K01, F-K02, F-K03, C3 |

---

## SOLID

### F-S01
*`build_daemon` is a 927-line composition root with 58 branches* (P0) — **Partly closed.**
Seven subsystem builders were extracted, and `build_daemon` went from 927 lines
to 526 today, still far over the old target of 200. → `R-RDB-02`.
**At W8: closed.** `build_daemon` is 62 lines over subsystem builders (`R-RDB-02`).

### F-S02
*Adding a master agent means writing a fourth copy of a build function* (P0) — **Closed.**
Profiles are data (`core/agent_profiles.py`), and
`test/core/test_fourth_master_cost.py` proves a new master costs a few lines.
Residual: the orchestrator still hand-wires two policies → `R-SOL-04`.
**At W8: closed**, residual included: the orchestrator's policies now come from its profile (`R-SOL-04`).

### F-S03
*`Store` is a 30-method god object spanning 8 domains* (P1) — **Closed.**
Consumers declare narrow protocols (tracker step 2.7).

### F-S04
*Backend selection is a 13× repeated conditional* (P1) — **Closed.**
`storage/factory.py` resolves the backend once; the tracker records 13 branches
down to 0 (step 2.6).

### F-S05
*Dependency types erased to `object | None` to break import cycles* (P1) — **Partly closed.**
`ports/` exists and is a guarded leaf. `OrchestratorDeps` dropped from 11 untyped
fields to 0. The master builders and `AgentBundle` still take `Any`, and the
module cycles remain. → `R-SOL-04`, `R-LAY-01`.
**At W8: partly closed.** The cycles are gone (`R-LAY-01`); the master builders' dependencies are still typed `Any` (`R-SOL-04`, carried).

### F-S06
*Two competing registries decide "which LLM provider"* (P1) — **Carried.**
Adding a provider still touches `core/config.py`, `context/config.py`,
`core/pricing.py` and `llm/providers/`. → `R-SOL-02`.
**At W8: partly closed.** Providers declare their context window; the live price lookup still guesses the provider from `base_url` (`R-PLC-10`, carried).

### F-S07
*`RoutedStore` is a typeless proxy applied to 3 of 17 stores* (P1) — **Closed.**
Failover is declared per domain in the factory (step 2.6).

### F-S08
*Five stores are reached through global mutable singletons* (P1) — **Partly closed.**
The tracker records 91 call sites reduced to 34. Seven `set_default_*` functions
and 46 accessor calls remain. → `R-GLB-01`.
**At W8: closed.** No `set_default_*` and no default-accessor calls remain (`R-GLB-01`).

### F-S09
*`core/config.py` is a god module* (P1) — **Carried.**
825 lines holding LLM, Docker, events, daemon and search configuration. → `R-PLC-02`.
**At W8: closed.** `core/config.py` is gone: LLM settings live in `llm/`, events and daemon configuration in their own packages (`R-PLC-02`).

### F-S10
*Store twins have divergent transaction and failure semantics* (P1) — **Closed for migrated domains.**
One dialect-level transaction policy, a rollback suite, and
`test/storage/test_twin_conformance.py` green. The unmigrated `usage` domain is
tracked under F-D02.

### F-S11
*`converse` is a 740-line endpoint at cyclomatic ~116* (P1) — **Carried, and worse:** 771 lines, ≈130. → `R-RDB-01`.
**At W8: closed.** `converse` is a 105-line router over one handler per frame type (`R-RDB-01`).

### F-S12
*Interfaces are sized to their implementation* (P2) — **Partly closed.**
Done for storage (step 2.7). The same problem recurs in renderers and settings. → `R-SOL-03`.
**At W8: partly closed.** Renderers share `ChatRendererProtocol`; the settings half is carried (`R-SOL-03`).

## DRY

### F-D01
*Three agent builders encode the same assembly sequence* (P0) — **Partly closed.**
Profile-driven shared helpers (injectors, async block, context tools, policies),
guarded by the fingerprint gate. The docker/local branch duplication and the
orchestrator's hand-wired policies remain. → `R-KIS-02`, `R-SOL-04`.
**At W8: closed.** One docker/local build path, and the orchestrator's policies from its profile (`R-KIS-02`, `R-SOL-04`).

### F-D02
*17 domains × 2 backends: the same rules written twice* (P0) — **Partly closed.**
18 of 19 domains migrated; twin pairs are down from 19 to 1. The `usage` domain
was paused by decision because its backends disagree on `task_id` handling —
a data-semantics question for the owner. → carried in [11](11-remediation-plan.md#carried).
**At W8: unchanged** — the `usage` domain waits for the owner's decision (carried).

### F-D03
*Two parallel graph-driving loops* (P1) — **Closed.**
One `_drive_graph` with two sinks; `test/core/test_driver_parity.py` green.

### F-D04
*Two competing schema-ownership mechanisms* (P1) — **Carried.**
`CREATE TABLE` still appears in 16 modules against a target of at most 2.
→ [11](11-remediation-plan.md#carried).
**At W8: carried** — still 16 modules.

### F-D05
*Four hand-maintained lists of the same provider set* (P2) — **Carried** with F-S06. → `R-SOL-02`.
**At W8: partly closed** with F-S06.

### F-D06
*Agent stack assembly duplicated across CLI, daemon and tinker* (P1) — **Partly closed.**
One storage decision is now shared (`StoreFactory.context_stores()`). The
async-host closure is still copied and has drifted. → `R-DRY-05`.
**At W8: closed.** The async-host builder is shared (`R-DRY-05`).

## KISS

### F-K01
*12 functions over 200 lines; peak cyclomatic 116* (P1) — **Carried:** 11 functions, peak ≈130. → `R-RDB-01`, `R-RDB-02`.
**At W8: closed on W4's terms.** 0 functions over 200 lines; peak complexity 25 (`R-RDB-02`).

### F-K02
*Parameter lists of 32, 30, 25 and 25* (P1) — **Carried, and grown:** 33, 32, 25 and 25. → `R-RDB-03`.
**At W8: closed.** `build_cli_deepagent` 33 → 7, `build_tinker_agent` 32 → 6, `create_app` 25 → 3 (`R-RDB-03`); the widest signature left is 12.

### F-K03
*Import placement is load-bearing behaviour* (P1) — **Carried, and now demonstrably so.**
The deferred-import ratio rose from 23.7% to 24.6%, and six modules crash when
imported first. → `R-IMP-01`, `R-IMP-02`, `R-INIT-01`.
**At W8: closed.** 0 modules fail when imported first; the deferred ratio is 1.2%, and every remaining deferred import states its reason (`R-IMP-01`, `R-IMP-02`, `R-INIT-01`).

### F-K04
*Dead and half-built structure left in place* (P2) — **Partly closed.**
The empty directory was removed and `agents/orchestrator/spawn.py` gained a
do-not-register banner. The module itself, its role entry and more dead code
remain. → `R-PLC-11`.
**At W8: closed.** `agents/orchestrator/spawn.py`, its role entry and the other dead code were deleted (`R-PLC-11`).

## Third-party coupling

### F-T01
*The cross-cutting-concern architecture is LangChain's `AgentMiddleware`* (P0) — **Closed.**
All 14 policies are plain `Policy` objects behind one `LangChainPolicyAdapter`.
Only the adapter and the compaction middleware still subclass `AgentMiddleware`
(tracker steps 4.4 and 4.7).

### F-T02
*`BaseChatModel` is the system's universal currency* (P1) — **Closed.**
`ModelHandle` (step 4.1), which also exposed and fixed the blank Azure model name.

### F-T03
*`create_deep_agent` is the only way the system builds an agent* (P1) — **Partly closed.**
The driving half is closed by the `Agent` protocol and one driver loop (step 4.5).
The constructing half is open by design and asserted by
`test_the_constructing_half_of_F_T03_is_still_open`. → [11](11-remediation-plan.md#carried).
**At W8: unchanged**, by design (carried).

### F-T04
*Load-bearing dependencies on `deepagents` internals* (P0) — **Closed.**
Negative-controlled tripwires; `scripts/verify_framework_contract.py` 8/8.

### F-T05
*Zero upper version bounds on framework dependencies* (P0) — **Closed.**
Every framework dependency is capped; the contract script's
"declared dependency ranges are sane" check passes.

### F-T06
*LangGraph's `interrupt()` called from inside domain code* (P1) — **Partly closed.**
The permission path uses the `AskUser` port (step 4.3). Carry-over C3, a tripwire
for the interrupt/resume protocol, is still open. → C3.
**At W8: unchanged** — C3 is carried.

### F-T07
*Middleware imports target a submodule path* (P2) — **Closed.**
All sites use the public path (step 0.4). The finding's other half, about hook
generations, was retracted as incorrect during execution.

## Carry-overs

### C1
*Replace `backend=` factory callables with protocol instances* — **Closed.**
The contract check "no build path reintroduced a backend factory" passes.

### C2
*Migrate the `build_agent` alias callers* — **Closed** in Phase 1.

### C3
*No tripwire for the LangGraph interrupt/resume protocol* — **Carried.**
→ [11](11-remediation-plan.md#carried).
**At W8: carried.**

## Decisions

The four ADRs still explain why the current code is shaped the way it is. At
close-out (W8) they move to `docs/architecture/decisions/`, unchanged except for
a status line.

### ADR-001
*Agent build pipeline* — implemented incrementally: profiles as data plus shared
helpers, not a parallel pipeline and cut-over. The tracker records this as a
deliberate revision.

### ADR-002
*Storage mapper layer* — implemented for 18 of 19 domains. Schema centralization
(F-D04) and `usage` remain.

### ADR-003
*Composition-root modules* — implemented in part. `ports/`, typed
`OrchestratorDeps` and the subsystem builders landed. Singletons (F-S08) and the
size target (F-S01) remain.

### ADR-004
*Framework boundary* — approved by the owner on 2026-08-10 and executed through
step 4.8. The constructing half of F-T03 remains.

## Old exit criteria against today

The structural review's remediation plan set these phase-exit targets. The phases
were marked complete; the numbers show which targets were met.

| Criterion | Target | At `f4ec45c` | Met | At W8 | Met |
|---|---|---|---|---|---|
| Framework dependencies with ceilings | all | all | yes | all | yes |
| One middleware import path | 1 | 1 | yes | 1 | yes |
| Fingerprint equivalence across configurations | green | 13 tests green | yes | green, path-normalised | yes |
| Store-selection sites in `bootstrap.py` | 1 | 0 branches (factory) | yes | 0 | yes |
| Modules containing `CREATE TABLE` | ≤ 2 | 16 | **no** | 16 | **no** (carried) |
| Functions over 200 lines in `daemon/bootstrap.py` | 0 | 1 (`build_daemon`, 526) | **no** | 0 (`build_daemon` 62) | yes |
| Deferred-import ratio | < 8% | 24.6% | **no** | 1.2% | yes |
| `set_default_*` globals | 0 | 7 | **no** | 0 | yes |
| Highest cyclomatic complexity | ~40 | ≈130 | **no** | 25 | yes |
| `core/engine.py` size | < 400 lines | 1,687 | **no** | 1,496 | **no** |

Each unmet row is covered by a finding in this review or listed as carried in
[11](11-remediation-plan.md#carried). At W8 two remain: schema centralization
(carried) and `core/engine.py`, which now holds only agent building (its CLI
code, policies and pricing moved out in W3) but is still three times the old
target; splitting it further is not in any wave.
