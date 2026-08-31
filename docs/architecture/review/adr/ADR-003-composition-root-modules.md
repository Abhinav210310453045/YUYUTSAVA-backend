# ADR-003 — Extract `ports/` and split the composition root into subsystem builders

**Status:** Proposed
**Addresses:** [`F-S01`](../02-findings-solid.md#f-s01), [`F-S05`](../02-findings-solid.md#f-s05), [`F-S08`](../02-findings-solid.md#f-s08), [`F-K01`](../03-findings-dry-kiss.md#f-k01), [`F-K03`](../03-findings-dry-kiss.md#f-k03), [`F-D06`](../03-findings-dry-kiss.md#f-d06)
**Phase:** 3 · **Requires:** ADR-001, ADR-002

---

## Context

Two problems that look separate and are the same problem.

**1. `build_daemon` is a 927-line function with 58 branches**
(`daemon/bootstrap.py:291`). It is the DI container, migration runner,
feature-flag evaluator, backend selector, agent factory, and lifecycle owner. The
knowledge of how the system fits together is expressed as *statement order inside
one function body* — unverifiable by any tool, unholdable in any head.

**2. Dependency types are erased to `object | None`.** `OrchestratorDeps` has ~10
such fields with the real type in a comment; `BaseSubAgent.__init__` has three
more, annotated *"untyped to avoid cycle"*. 220 internal imports (23.7%) are
deferred inside function bodies; `core/engine.py` alone has 68.

These share a root cause: **there is no acyclic layering.** `core/` imports from
`context/`, `memory/`, `skills/`, `daemon/`, `agents/`; all of those import from
`core/`. There is no direction in which a type can be declared, so types are
erased and imports are deferred. And because no module can safely import another
at load time, everything must be constructed in one place — which is why
`build_daemon` is 927 lines.

## Decision

### 1. Extract `yuyutsava/ports/` — the dependency-free protocol package

```
yuyutsava/ports/
├── __init__.py
├── storage.py     # MemoryStore, ArtifactStore, SummaryStore, TranscriptStore, SkillStore
├── policy.py      # CapEnforcer, ConsentRegistry, PermissionsPolicy
├── async_tasks.py # AsyncTaskMirror, AsyncHost
└── retrieval.py   # Injector, VectorSearch
```

**One rule, enforced in CI:** `ports/` imports nothing from `yuyutsava` except
`ports` itself.

Both sides of every cycle import `ports`; neither imports the other. The cycles
disappear *structurally* rather than being evaded. The ~10 `object | None` fields
become real types, and most of the 220 deferred imports collapse.

This is what makes `F-S05` fixable at all. Without it, every other fix is
cosmetic — the type erasure is a *consequence* of the cycles, not a style choice.

### 2. Split `build_daemon` into subsystem builders

```python
async def build_daemon(opts: DaemonOptions) -> DaemonSubsystems:
    storage   = await build_storage(opts)
    retrieval = await build_retrieval(opts, storage)
    agents    = await build_agents(opts, storage, retrieval)
    web       = await build_web(opts, storage, agents)
    return DaemonSubsystems(storage, retrieval, agents, web)
```

Each builder has explicit inputs and outputs, is under 150 lines, and is
independently testable. The dependency order that is currently implicit in
statement sequence becomes explicit in the parameter lists.

This is only tractable *after* ADR-002 removes the 13 backend branches and
ADR-001 removes the builder duplication — which is why this ADR is sequenced
third.

### 3. Retire the global singletons

Replace the five `set_default_*` / `get_default_*` pairs (`F-S08`) with an
explicit `AppContext` threaded through call sites:

```python
async def purge_session(ctx: AppContext, session_id: str) -> PurgeReport: ...
```

`purge_session` currently takes one argument and touches four stores through
globals. One extra parameter is a small price for a signature that is honest
about its blast radius — and for tests that need no global setup/teardown.

### 4. Enforce the layering in CI

```toml
# import-linter contract
[[tool.importlinter.contracts]]
name = "ports is a leaf"
type = "forbidden"
source_modules = ["yuyutsava.ports"]
forbidden_modules = ["yuyutsava.core", "yuyutsava.daemon", "yuyutsava.agents",
                     "yuyutsava.storage", "yuyutsava.context", "yuyutsava.memory"]
```

Plus a metric gate on the deferred-import ratio so it cannot regress past 8%.
**Without the CI contract this decision decays**, because the pressure that
created the cycles is still present.

## Alternatives considered

### A. `TYPE_CHECKING` imports instead of a ports package

```python
if TYPE_CHECKING:
    from yuyutsava.memory.store import MemoryStore
```

**Rejected as insufficient.** It fixes the *annotations* (mypy sees real types)
without fixing the *architecture* — the runtime cycles remain, the 220 deferred
imports remain, `build_daemon` still must construct everything in one place, and
import order stays load-bearing.

Worth doing as an interim improvement if Phase 3 is deferred. It is strictly
better than `object | None`. It is not a substitute.

### B. A DI framework (`dependency-injector`, `wired`)

**Rejected.** It would replace an explicit 927-line function with an implicit
container — harder to trace, not easier. The problem is not that wiring is manual;
it is that wiring is *undifferentiated*. Named subsystem builders solve that
without adding a framework, which also keeps the composition root free of the
framework coupling this review is otherwise trying to reduce.

### C. Split `bootstrap.py` into modules without extracting `ports/`

**Rejected.** The split would not hold. Without an acyclic layer, the subsystem
builders must still import each other's concrete types, recreating the cycles
that forced the single-function shape. `ports/` is the enabling step; the split
is the payoff.

### D. Keep the singletons for ergonomics

**Partially accepted.** `purge_session`'s zero-wiring call is genuinely
convenient, and the convenience argument in `storage/purge.py:113-115` is
reasonable. The counter-argument that wins: hidden dependencies at the
*persistence* boundary cost test isolation and forbid multiple instances per
process. `AppContext` preserves ~90% of the ergonomics at none of the cost.

## Consequences

### Positive

- Onboarding stops at one 150-line builder instead of one 927-line function.
- Subsystems become independently testable — today, testing 20 lines of daemon
  wiring requires constructing an entire daemon.
- Real types at the system's most important seams; IDE navigation works where
  newcomers need it most.
- Deferred-import ratio 23.7% → < 8%; the import graph becomes the dependency
  graph, so static analysis and architecture lint become meaningful.
- Tests need no global setup/teardown for store access.

### Negative

- `ports/` adds a package that must be kept genuinely dependency-free. **The CI
  contract is not optional** — without it this decays back within two quarters.
- Threading `AppContext` touches many call sites. Mechanical, but broad.
- Protocol definitions and implementations live in different packages, so
  "jump to implementation" gains a hop.
- ~2 weeks, no user-visible benefit.

### Risk and mitigation

| Risk | Mitigation |
|------|-----------|
| `ports/` accumulates dependencies and stops being a leaf | import-linter contract in CI from day one, not retrofitted |
| Boot-order regressions during the split | Subsystem builders have explicit parameters — a missing dependency becomes a `TypeError` at import time rather than a runtime `None` |
| `AppContext` becomes a new god object | It holds *only* subsystem handles, never behavior. Lint against methods on it |
| Merge conflicts in `bootstrap.py` during the split | Land the split as one focused change; coordinate so no feature work touches `bootstrap.py` that week |

## Verification

- import-linter contract green
- Deferred-import ratio < 8% (measure with the `01 § M4` command)
- Zero `object | None` dependency fields
- No function in `daemon/bootstrap.py` over 200 lines
- Each subsystem builder has a unit test that constructs it in isolation
