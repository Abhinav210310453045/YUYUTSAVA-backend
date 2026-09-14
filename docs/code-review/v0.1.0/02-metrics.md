# 02 — Metrics

Every number in this review, the command that produced it, and its target.
Reviewed commit: `f4ec45c`. The *old* column is the structural review of
2026-08-08 wherever it measured the same thing; `—` means it did not.
The *after* column is filled in at close-out (W8).

## Reproducing

```bash
.venv/bin/python scripts/measure_code_health.py --details 20   # AST and text metrics; never imports the package
.venv/bin/python scripts/verify_imports.py --slowest 12        # every module, imported first and alone
uvx ruff@0.16.5 check yuyutsava --select <RULES> --statistics
uvx ruff@0.16.5 format --check yuyutsava test scripts
```

`measure_code_health.py` approximates cyclomatic complexity as branch nodes + 1,
counting nested functions, to stay comparable with the old review. ruff's
`C901` is the gate.

## Python package — `yuyutsava/`

| Metric | Old | Now | Target | After |
|---|---|---|---|---|
| Modules | 350 | 387 | — | |
| Lines | 55,601 | 66,953 | — | |
| Functions | — | 2,590 | — | |
| **Modules that fail when imported first** | — | **6** | 0 | |
| Function-body imports | — | 415 | reasoned only | |
| — of which first-party / third-party / stdlib | — | 288 / 101 / 26 | —/—/0 | |
| Deferred first-party import ratio | 23.7% | 24.6% | < 8% | |
| Module-level imports after executable code | — | 31 | 0 | |
| Module-level imports after the first `def`/`class` | — | 5 | 0 | |
| Cross-package imports of private names | — | 14 | 0 | |
| Largest sub-package import cycle | — | 21 of 31 | no cycle through `core`, `llm`, `storage` | |
| Two-way sub-package dependencies | — | 9 | 0 | |
| Functions over 200 lines | 12 | 11 | 0 | |
| Functions over 100 lines | — | 38 | ≤ 10 | |
| Functions over 60 lines | — | 88 | falling | |
| Functions with more than 10 parameters | 16 | 14 | ≤ 3 | |
| Functions with more than 7 parameters | — | 38 | falling | |
| Highest cyclomatic complexity (≈) | 116 | 130 | ≤ 25 | |
| Functions with complexity above 25 | — | 13 | 0 | |
| Functions with complexity above 15 | — | 58 | shrinking allowlist | |
| Classes over 400 lines | — | 9 | 0 | |
| Broad handlers that do nothing | — | 58 | 0 | |
| `print` calls outside `cli/` | — | 86 | 0 | |
| `global` statements | — | 25 | documented only | |
| `set_default_*` singletons | 5 pairs | 7 | 0 | |
| Default-accessor call expressions | 91 (tracker) | 46 | 0 | |
| History-narration phrases | — | 176 in 108 files | 0 (reviewed) | |
| Modules containing `CREATE TABLE` | 16 | 16 | ≤ 2 (carried) | |
| Modules importing a framework directly | 64 (18.3%) | 66 (17.1%) | — | |
| `__init__.py` without a docstring | — | 5 | 0 | |
| `__init__.py` containing logic | — | 1 | 0 | |
| `__init__.py` exporting unused names | — | 24 | 0 | |

The old *default-accessor* figure came from the old tracker's own count
(call sites including references); the new one counts call expressions only, so
the two are not strictly comparable.

## Import cost

Measured with `.venv/bin/python -c "import time,sys; t=time.perf_counter(); import <module>; …"`
on a warm file cache (cold figures from the first run in brackets).

| Import | Time | Modules loaded | LangChain loaded | Target |
|---|---|---|---|---|
| `yuyutsava.core.config` | 532 ms (860) | 2,398 | yes | < 100 ms, no |
| `yuyutsava.llm.providers` | 651 ms (1,314) | 2,987 | yes | — |
| `yuyutsava.memory.store` | 559 ms | 2,519 | yes | — |
| `yuyutsava.context.config` | 555 ms | 2,524 | yes | < 100 ms, no |
| `yuyutsava.storage.paths` | 7 ms | 94 | no | unchanged |

Slowest individual imports (`verify_imports.py --slowest`): `daemon.bootstrap`
1,703 ms, `daemon.main` 1,685 ms, `daemon.web.server` 1,415 ms,
`cli.commands.chat_repl` 1,395 ms, `llm` 1,393 ms.

## Lint — ruff 0.16.5

Selected rules over `yuyutsava/`:

| Rule | Meaning | Count |
|---|---|---|
| `PLC0415` | import outside top level | 410 |
| `T201` | `print` | 259 |
| `F401` | unused import | 121 |
| `I001` | unsorted imports | 84 |
| `PLR0913` | too many arguments | 77 |
| `BLE001` | blind `except` | 67 |
| `B008` | function call in a default argument | 61 |
| `C901` | too complex | 57 |
| `SIM105` | suppressible exception | 55 |
| `S110` | `try`/`except`/`pass` | 50 |
| `F821` | undefined name | 35 |
| `PLR0912` / `PLR0915` | too many branches / statements | 24 / 22 |
| `B904` | `raise` without `from` in `except` | 13 |
| `ERA001` | commented-out code | 9 |
| `F841` / `F822` / `E741` / `F541` / `E402` | bug-class | 4 / 2 / 2 / 1 / 3 |

ruff's own `E402` finds 3 where the AST scan finds 31, because ruff exempts
imports that follow `os.environ` changes. The standard does not.

Over `test/` and `scripts/`: `PLC0415` 469, `T201` 82, `I001` 39, `E402` 15,
`F401` 13, `BLE001` 7, `S110` 2, `F841` 2.

`ruff format --check`: 360 of 563 Python files would be reformatted.

## Tests and scripts

| Metric | Now | Target |
|---|---|---|
| Test files | 145 | — |
| Files using `unittest` / importing `pytest` | 115 / 2 | one runner |
| `conftest.py` | 0 | 1 plus `test/support/` |
| `sys.path` manipulations in `test/` and `scripts/` | 10 | 0 |
| Private-name imports in tests | 16 | 0 |
| Fake classes duplicated across files | 7 names (up to ×4) | 0 |
| Scripts documented in a README | 0 of 8 | all |
| Hermetic test subset run in CI | no | yes |

## Electron app

| Metric | Now | Target |
|---|---|---|
| Lines (JS, JSX, CSS, HTML) | 15,822 | — |
| Lint / format / type / test tooling | none | ESLint, Prettier, vitest |
| Components over 500 lines | 4 | 0 (≤ 300 each) |
| Largest file — `conversations/store.js` | 922 (one class, ~800) | ≤ 300 per module |
| `fetch` calls outside `src/renderer/api/` | 5 | 0 |
| Dead duplicate components | 1 | 0 |
| Most `style={{…}}` in one file | 40 (`ChatPanel.jsx`) | computed values only |

## Signals not used

`TODO`/`FIXME` markers (87) are **not** a hygiene signal here: the regex also
matches the product's "TODO board", which dominates the count.

## Gate baselines at `f4ec45c`

Recorded before any fix, and re-run after every wave.

| Gate | Result |
|---|---|
| `test/core/test_agent_fingerprint.py` | 13 tests OK |
| `test/core/test_agent_profiles.py` | 8 tests OK |
| `test/test_ports_is_a_leaf.py` | 8 tests OK |
| `scripts/verify_framework_contract.py` | 8/8 contracts hold |
| `scripts/verify_loop_affinity.py` | 19 passed |
| `scripts/verify_imports.py` | 387 modules, 6 failures (`R-IMP-01`) |
| `yuyutsava --print-tools` output (sha256) | `08021171db6d…` |
