# Code Review v0.1.0

A strict, code-level review of YUYUTSAVA at commit **`f4ec45c`** — `main` on
2026-09-14, immediately after the context-telemetry work merged.

It asks, for every dimension: **would a careful engineer joining this project
find the code readable, correctly placed, and cheap to change?** Where the answer
is no, it says exactly where, proves it with a reproducible measurement, and
prescribes the fix.

> **Stance.** Findings come from what the code *does* — its import graph, its
> syntax trees, how its modules actually import, lint output — not from what its
> comments claim. A comment that contradicts the code is itself a finding
> (`R-DOC`). Nothing is marked fixed without a recorded verification.

## Scope

| In scope | Size at `f4ec45c` |
|---|---|
| `yuyutsava/` — the Python runtime | 387 modules, 66,953 lines, 31 sub-packages |
| `test/` and `scripts/` | 145 test files, 8 scripts |
| `electron-app/` — the desktop app | ~15.8k lines of JS/JSX/CSS |

**Not assessed:** runtime performance, security, product behaviour, and the
content of prompts. The system works and is used daily; the question is whether
its code will keep letting it grow.

## Relationship to the structural review

The structural review of 2026-08-08 (formerly `docs/architecture/review/`) evaluated the architecture — agent
builders, storage twins, the composition root, framework coupling — and its
remediation plan was largely executed. This review works one level down, at the
code itself, and **supersedes** it:

- every finding of the old review is closed or carried forward here, by ID, in
  [10-legacy-ledger.md](10-legacy-ledger.md);
- its four ADRs moved to [`docs/architecture/decisions/`](../../architecture/decisions/README.md)
  because they still explain why the current code is shaped the way it is, and
  its storage migration playbook to
  [`docs/architecture/storage-migration-playbook.md`](../../architecture/storage-migration-playbook.md);
- the old review directory was deleted at W8, once this review's fixes had landed.

## How to read

| # | Document | Answers |
|---|---|---|
| [00](00-executive-summary.md) | Executive summary | The verdict per dimension, the top risks, the order of attack |
| [01](01-standards.md) | Standards | The rules code in this repository must follow, and how each is enforced |
| [02](02-metrics.md) | Metrics | Every number, the command that produced it, and its target |
| [03](03-imports-and-packages.md) | Imports & packages | Import placement, circular imports, `__init__.py` contents |
| [04](04-placement-and-layering.md) | Placement & layering | Which code lives in the wrong package, and the dependency direction |
| [05](05-readability-kiss-docs.md) | Readability, KISS, docs | Oversized functions and classes, needless complexity, misleading comments |
| [06](06-solid-dry.md) | SOLID & DRY | Responsibilities, extension points, abstractions, duplication |
| [07](07-errors-logging-state.md) | Errors, logging, state | Swallowed exceptions, output, global state |
| [08](08-tests-and-scripts.md) | Tests & scripts | Test layout, runners, shared fixtures, what is untested |
| [09](09-electron-app.md) | Electron app | Renderer structure, API access, component size, tooling |
| [10](10-legacy-ledger.md) | Legacy ledger | Status of every finding from the structural review |
| [11](11-remediation-plan.md) | Remediation plan | The waves of fixes, their exit criteria and verification |

Read `00` first. Engineers changing code should read `01`.

## Finding IDs

Every finding has a stable ID, is stated once in its home document, and is
referenced by ID everywhere else.

| Prefix | Dimension | Home |
|---|---|---|
| `R-IMP` | Import placement and hygiene | 03 |
| `R-INIT` | Package `__init__.py` contents | 03 |
| `R-LAY` | Layering and import cycles | 04 |
| `R-PLC` | Code placement and cohesion | 04 |
| `R-RDB` | Readability (size, shape, naming) | 05 |
| `R-KIS` | Unearned complexity | 05 |
| `R-DOC` | Docstrings and comments that mislead | 05 |
| `R-SOL` | SOLID violations | 06 |
| `R-DRY` | Duplicated code or knowledge | 06 |
| `R-ERR` | Error handling | 07 |
| `R-LOG` | Output and logging | 07 |
| `R-GLB` | Global and hidden state | 07 |
| `R-BUG` | Behaviour defects found while reviewing | 07 |
| `R-TST` | Tests | 08 |
| `R-SCR` | Scripts | 08 |
| `R-UI` | Electron app | 09 |

## Finding format

Each finding records:

- **Claim** — the defect in one sentence.
- **Evidence** — `path:line` references and the command that measured it.
- **Consequence** — what concretely goes wrong, as a scenario.
- **Fix** — what to change.
- **Verification** — how "fixed" is proven.
- **Severity**, **Wave** (from [11](11-remediation-plan.md)) and **Status**.

Paths are written as code, not links, so that moving a module never breaks the
documentation link check.

## Severity

| Severity | Meaning |
|---|---|
| **P0** | Causes defects or crashes, creates import cycles, or blocks a planned change. |
| **P1** | Compounding cost: every future change in the area is slower or riskier. |
| **P2** | Local hygiene. Real, bounded, non-compounding. |

## Status

`Open` · `Fixed` (with the commit) · `Carried` (with the reason and where it
goes next) · `Won't fix` (with the reason).
