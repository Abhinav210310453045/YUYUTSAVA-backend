# 00 — Executive Summary

**Scope:** `yuyutsava/` — 350 modules, 55,601 lines. Branch `yuyutsava-daemon` @ `f3fa86d`.
**Criterion:** extensibility & modularity (P0), evaluated through SOLID / DRY / KISS.

---

## Verdict

**The system has good *intentions* and a weak *spine*.**

The codebase is not naive. It contains real abstractions that a careless project
would lack: a `Provider` registry for LLM backends, a `BaseSubAgent` ABC, a
`RoutedStore` spillover proxy, a reusable `yuyutsava/retrieval/` engine, a
`yuyutsava/platform/` OS-isolation layer. Someone has been thinking about design.

The problem is that **these abstractions stop at the edge of the system's three
hot paths** — *building an agent*, *persisting a domain*, and *booting the
process* — and those three paths are exactly where all growth happens. Every new
feature lands in the part of the codebase with no abstraction, so the good
abstractions elsewhere do not lower the cost of change.

The result is a codebase where **adding a capability is cheap to imagine and
expensive to land**. The measured cost of the three most common changes:

| Change | Files to edit | Lines to write | Copy-paste required? |
|--------|---------------|----------------|----------------------|
| Add a new master agent | 1 + call sites | ~250 (a 4th copy of an existing function) | **Yes** |
| Add a new persisted domain | 6–8 | ~400 (2 hand-written SQL backends) | **Yes** |
| Add a new LLM provider | 3 | ~90 | Partly |

Full derivation in [05-change-cost-scenarios.md](05-change-cost-scenarios.md).

---

## The single most important structural fact

> **There are 17 persisted domains. Each is implemented as three classes — an
> ABC, a SQLite implementation, and a Postgres implementation — for a total of
> 51 hand-maintained store classes, with the same business rules written twice
> in two SQL dialects.**

This one decision (`F-D02`) accounts for the largest share of the codebase's
mass, its slowest change path, its highest divergence risk, and a large fraction
of the branching in the composition root. It is also the most mechanically
fixable, because the two implementations are *structurally parallel* — same
method names, same order, same contracts — which is precisely the shape that a
mapper layer collapses.

---

## Scorecard

Graded against the P0 criterion. "Extension cost" = what it takes to add the
next thing in that dimension.

| Principle | Grade | One-line assessment |
|-----------|-------|---------------------|
| **Single Responsibility** | **D** | Three god functions (927, 740, 567 lines); `core/config.py` holds 13 provider dataclasses *and* limits *and* events *and* daemon config. |
| **Open/Closed** | **D** | The three highest-traffic extension points (master agents, persisted domains, backend selection) are all closed — extension means editing, not adding. |
| **Liskov Substitution** | **C** | Store twins have divergent transaction semantics; `RoutedStore` is a `__getattr__` proxy that is not a subtype of anything. |
| **Interface Segregation** | **D** | `Store` exposes 30 methods across 8 unrelated domains to 14 consumers; `TodoStore` is a single 21-method interface. |
| **Dependency Inversion** | **D+** | Direction is right in `llm/` and `retrieval/`; elsewhere ~10 dependency fields are typed `object \| None` to break import cycles, and 5 stores are reached through global mutable singletons. |
| **DRY** | **D** | 3 near-identical agent builders; 17 duplicated store pairs; 2 parallel streaming loops; schema DDL in 16 files *and* a central migration file. |
| **KISS** | **C−** | 30 functions over 100 lines, 12 over 200; one endpoint at cyclomatic ~116; one function with 32 parameters. |
| **Third-party insulation** | **D−** | 14 middleware classes extend a LangChain base class directly; `BaseChatModel` is the universal currency; the code depends on *undocumented internals* of `deepagents` with no version ceiling. |

**Composite: D+.** The system is well-intentioned and internally consistent, but
its structure is optimized for the code that already exists, not for the code
that comes next.

---

## Top 10 risks, ranked by compounding cost

Ranked by *(frequency of the change it obstructs)* × *(cost per change)* ×
*(likelihood of silent divergence)*.

| # | Finding | Title | Severity |
|---|---------|-------|----------|
| 1 | [`F-D02`](03-findings-dry-kiss.md#f-d02) | 17 store domains implemented twice, by hand, in two SQL dialects | **P0** |
| 2 | [`F-D01`](03-findings-dry-kiss.md#f-d01) | Three agent build functions are structural copies of one another | **P0** |
| 3 | [`F-T01`](04-findings-thirdparty-coupling.md#f-t01) | The entire cross-cutting-concern architecture is 14 direct subclasses of LangChain's `AgentMiddleware` | **P0** |
| 4 | [`F-S01`](02-findings-solid.md#f-s01) | `build_daemon` is a 927-line, 58-branch single function that is the only composition root | **P0** |
| 5 | [`F-T04`](04-findings-thirdparty-coupling.md#f-t04) | Code depends on `deepagents` *internals* (line-numbered behavior, a private prompt constant, string matching) with **no upper version bound** | **P0** |
| 6 | [`F-S05`](02-findings-solid.md#f-s05) | `OrchestratorDeps` / `BaseSubAgent` erase ~10 dependency types to `object \| None` to break import cycles | **P1** |
| 7 | [`F-S03`](02-findings-solid.md#f-s03) | `Store` is a 30-method god object spanning 8 domains, consumed by 14 modules including agents | **P1** |
| 8 | [`F-D04`](03-findings-dry-kiss.md#f-d04) | Two competing schema-ownership mechanisms: DDL in 16 modules *and* a 866-line central migration file | **P1** |
| 9 | [`F-S07`](02-findings-solid.md#f-s07) | `RoutedStore` is documented as the universal spillover proxy but wired for only 3 of 17 stores | **P1** |
| 10 | [`F-K01`](03-findings-dry-kiss.md#f-k01) | 12 functions exceed 200 lines; `converse` reaches cyclomatic ~116 | **P1** |

---

## What is genuinely good, and must survive the fix

A remediation plan that damages these would be a net loss. Each is a working
model for how the rest of the system *should* look.

- **`yuyutsava/llm/`** — `Provider` ABC + `_PROVIDERS` registry + isinstance
  dispatch + a `require()` helper for optional SDKs. Adding a provider is one
  module and one tuple entry. **This is the reference pattern for the whole
  codebase.** Its one defect (`F-S06`) is that a *second*, contradictory
  registry for the same concept lives in `core/config.py`.
- **`yuyutsava/retrieval/`** — a genuine reusable engine (`PgVectorTable`,
  `PgVectorSearch`) shared by memory, skills, and transcripts. Proof the team
  can build a shared substrate when it decides to.
- **`yuyutsava/platform/`** — OS-specific code isolated behind one boundary.
  Correct DIP, correctly applied.
- **`BaseSubAgent`** — a real ABC with three abstract members and useful
  provided behavior. Adding a *sub*agent is genuinely cheap. The contrast with
  adding a *master* agent (`F-S02`) is the clearest evidence that the team knows
  the right pattern and simply has not applied it to the master path.
- **`RoutedStore`** — the right idea, correctly generic. It is under-applied,
  not wrong.

---

## Recommended sequence

Full plan, with per-phase creation flow, in
[06-remediation-plan.md](06-remediation-plan.md). The headline:

| Phase | Theme | Addresses | Why this order |
|-------|-------|-----------|----------------|
| **0** | Freeze the bleeding | `F-T04`, `F-T05` | Version ceilings + characterization tests on framework seams. Days of work; prevents a silent upgrade break while everything else is in flight. |
| **1** | Agent build pipeline | `F-D01`, `F-S02`, `F-K02` | Collapse 3 builders into a composable spec. Highest leverage, self-contained, no storage risk. |
| **2** | Storage mapper layer | `F-D02`, `F-D04`, `F-S07` | Collapse 51 classes toward ~17 + 2 dialects. Largest mass reduction; do after Phase 1 so the composition root only changes once. |
| **3** | Composition root split | `F-S01`, `F-S05`, `F-D03` | `build_daemon` becomes a sequence of subsystem builders. Only tractable *after* 1 and 2 remove most of its branching. |
| **4** | Framework boundary | `F-T01`, `F-T02`, `F-T03` | An adapter seam for middleware and model types. Deliberately last — it is the largest change and the least urgent once Phase 0 caps the risk. |

**Phases 1–3 are worth doing regardless of whether Phase 4 is ever attempted.**
Phase 4 is a strategic bet on framework independence; the others are pure
cost-of-change reductions with no strategic precondition.

---

## The one-sentence summary

> The abstractions this codebase already has are good enough to prove the team
> knows how to build them — the work is not inventing new patterns, it is
> applying the patterns already present in `llm/` and `retrieval/` to the three
> paths where all the growth actually happens.
