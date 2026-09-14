# 00 — Executive summary

**Code Review v0.1.0** · reviewed commit `f4ec45c` (`main`, 2026-09-14) · scope
`yuyutsava/`, `test/`, `scripts/`, `electron-app/`

## Verdict

**Overall: C−.** The system works, and its hardest-won parts are genuinely good:
- a dependency-free protocol layer;
- negative-controlled tripwires on the agent framework;
- an agent-assembly fingerprint;
- a storage layer collapsed from nineteen hand-written twin pairs to one.

Around those parts, the everyday code has drifted in six ways:
- **Import cycles.** Imports hide circular dependencies; six modules crash if
  they are the first thing imported.
- **One tangle.** Twenty-one of thirty-one sub-packages form one import cycle.
- **Giant functions.** The biggest reach 771 lines and complexity ≈130.
- **Copied helpers.** Small helpers exist in up to eighteen disagreeing copies.
- **Silent failures.** Fifty-eight exception handlers swallow errors silently.
- **Misleading docs.** Docstrings narrate project history, or describe code that
  no longer exists.

None of it is a crisis today. All of it compounds: each change in these areas is
slower and riskier than it needs to be.

## Scorecard

| Dimension | Grade | Why |
|---|---|---|
| Imports & packages | **D** | 6 modules fail when imported first; 415 function-body imports, most without a reason; one eager `__init__.py` loads the whole agent stack for a settings import (`R-IMP-01`, `R-IMP-02`, `R-INIT-01`) |
| Layering & placement | **D** | 21 of 31 sub-packages in one cycle, 9 two-way dependencies; `core/` is a grab-bag; no rule for where tools or policies live (`R-LAY-01`, `R-PLC-01`, `R-PLC-05`) |
| Readability | **C−** | 11 functions over 200 lines; parameter lists of 33 and 32; 176 history-narration phrases (`R-RDB-01`…`03`, `R-DOC-02`) |
| KISS | **C** | duplicated branches, hand-dispatched subcommands, work done and undone (`R-KIS-01`, `R-KIS-02`) |
| SOLID | **C** | five god classes; adding a provider touches four files; renderers share no contract; builders still take `Any` (`R-SOL-01`…`04`) |
| DRY | **C−** | ~18 environment parsers, 4 content flatteners, copied rendering and HITL helpers (`R-DRY-01`…`06`) |
| Errors, logging, state | **C−** | 58 silent handlers, 86 library `print`s, 7 singletons, 4 confirmed defects (`R-ERR-01`, `R-LOG-01`, `R-GLB-01`, `R-BUG-*`) |
| Documentation accuracy | **C** | comments explain *why* unusually well, but 15+ contradict the code (`R-DOC-01`, `R-INIT-06`) |
| Tests | **B−** | outstanding tripwires and parity suites; no runner configuration, no CI test gate, copied fakes, untested packages (`R-TST-01`, `R-TST-06`) |
| Electron app | **C** | no lint or test tooling; an ~800-line store class; four components over 500 lines (`R-UI-01`…`03`) |

## What is already good — protect it

- **`yuyutsava/ports/`**, kept dependency-free by `test/test_ports_is_a_leaf.py`.
- **`test/core/test_agent_fingerprint.py`:** agent assembly is proven
  byte-identical across nine configurations. It is what makes the W3–W4
  refactors safe.
- **`scripts/verify_framework_contract.py`:** every silent framework coupling
  has a tripwire, and each tripwire was seen failing before it was trusted.
- **The storage dialect.** One implementation per domain, a conformance suite,
  and purge lists derived rather than hand-written.
- **The policy adapter:** fourteen framework subclasses became plain objects.
- **Framework dependency ceilings,** with the contract script as their
  acceptance test.
- **OS-specific code confined to `platform/`,** plus comments that record *why*
  a constraint exists.

## Top 15 risks

| # | Finding | Severity | In one line |
|---|---|---|---|
| 1 | `R-IMP-01` | P0 | Six modules crash when imported first; import clean-ups become crashes |
| 2 | `R-INIT-01` | P0 | `core/__init__.py` loads the agent stack for any `core` import and closes 11 cycles |
| 3 | `R-LAY-01` | P0 | One import cycle spans 21 of 31 packages; nothing is testable in isolation |
| 4 | `R-BUG-01` | P1 | Most providers compact at a 128k budget, discarding history their models could hold |
| 5 | `R-BUG-02` | P1 | A corrupt price file is overwritten, destroying hand-authored prices |
| 6 | `R-RDB-01` | P1 | `converse` — 771 lines, complexity ≈130 — sits on the path of planned voice work |
| 7 | `R-SOL-02` | P1 | Extension points are chains; one has already drifted into a dropped-visuals bug |
| 8 | `R-ERR-01` | P1 | 58 handlers swallow every exception without a trace |
| 9 | `R-IMP-02` | P1 | 415 function-body imports make dependencies invisible and ordering load-bearing |
| 10 | `R-SOL-01` | P1 | `TodoExchange`, the async watcher, the dashboard and the meter each do four jobs |
| 11 | `R-DOC-01` | P1 | Comments contradict code, including an agent's skill-writing default |
| 12 | `R-DRY-01` | P1 | `YUYUTSAVA_X=on` works for some flags and silently not others |
| 13 | `R-TST-01` | P1 | CI runs no tests because nothing marks which ones need Postgres or keys |
| 14 | `R-UI-01` | P1 | The desktop app has no lint or tests guarding its largest files |
| 15 | `R-GLB-01` | P1 | Seven stores reached through module globals |

## Order of attack

Detail in [11](11-remediation-plan.md):

| Wave | Focus |
|---|---|
| W0 | Safety net — done with this review |
| W1 | Guardrails |
| W2 | `__init__.py` and imports |
| W3 | Layering and placement |
| W4 | Readability, SOLID, DRY |
| W5 | Errors, state and defects |
| W6 | Tests |
| W7 | Electron |
| W8 | Close-out |

The single most valuable change is the first step of W2. Emptying
`core/__init__.py` removes the root cause of most cycles and deferred imports,
and makes every later import clean-up safe.
