# 02 — Metrics

Every number in this review, the command that produced it, and its target.
Reviewed commit: `f4ec45c`. The *old* column is the structural review of
2026-08-08 wherever it measured the same thing; `—` means it did not.
The *after* column was filled in at close-out (W8, 2026-09-28, commit `67523dce`).

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
| Modules | 350 | 387 | — | 478 |
| Lines | 55,601 | 66,953 | — | 72,261 |
| Functions | — | 2,590 | — | 2,902 |
| **Modules that fail when imported first** | — | **6** | 0 | **0** |
| Function-body imports | — | 415 | reasoned only | 52, each with a reason |
| — of which first-party / third-party / stdlib | — | 288 / 101 / 26 | —/—/0 | 16 / 34 / 2 (37 of them optional, under `try`) |
| Deferred first-party import ratio | 23.7% | 24.6% | < 8% | 1.2% |
| Module-level imports after executable code | — | 31 | 0 | 2, both deliberate (an optional backend under `try`; matplotlib's `Agg` before pyplot) |
| Module-level imports after the first `def`/`class` | — | 5 | 0 | 0 |
| Cross-package imports of private names | — | 14 | 0 | 0 |
| Largest sub-package import cycle | — | 21 of 31 | no cycle through `core`, `llm`, `storage` | none (largest component 1) |
| Two-way sub-package dependencies | — | 9 | 0 | 0 |
| Functions over 200 lines | 12 | 11 | 0 | 0 |
| Functions over 100 lines | — | 38 | ≤ 10 | 19: 8 accepted tool factories, the face-watcher's `main`, 10 builders (3 crossed 100 after W4) |
| Functions over 60 lines | — | 88 | falling | 79 |
| Functions with more than 10 parameters | 16 | 14 | ≤ 3 | 4 |
| Functions with more than 7 parameters | — | 38 | falling | 29 |
| Highest cyclomatic complexity (≈) | 116 | 130 | ≤ 25 | 25 |
| Functions with complexity above 25 | — | 13 | 0 | 0 |
| Functions with complexity above 15 | — | 58 | shrinking allowlist | 28 |
| Classes over 400 lines | — | 9 | 0 | 3 (`ChatDashboard` 450, `OrchestratorLoop` 423, `RichChatRenderer` 408) |
| Broad handlers that do nothing | — | 58 | 0 | 1 (the face watcher, off limits) |
| `print` calls outside `cli/` | — | 86 | 0 | 0 |
| `global` statements | — | 25 | documented only | 11 |
| `set_default_*` singletons | 5 pairs | 7 | 0 | 0 |
| Default-accessor call expressions | 91 (tracker) | 46 | 0 | 0 |
| History-narration phrases | — | 176 in 108 files | 0 (reviewed) | 2 in 2 files, both reviewed prompt strings |
| Modules containing `CREATE TABLE` | 16 | 16 | ≤ 2 (carried) | 16 (carried) |
| Modules importing a framework directly | 64 (18.3%) | 66 (17.1%) | — | 80 (16.7%) |
| `__init__.py` without a docstring | — | 5 | 0 | 0 |
| `__init__.py` containing logic | — | 1 | 0 | 0 |
| `__init__.py` exporting unused names | — | 24 | 0 | 0 |

The old *default-accessor* figure came from the old tracker's own count
(call sites including references); the new one counts call expressions only, so
the two are not strictly comparable.

## Import cost

Measured with `.venv/bin/python -c "import time,sys; t=time.perf_counter(); import <module>; …"`
on a warm file cache (cold figures from the first run in brackets).

| Import | Time | Modules loaded | LangChain loaded | Target | After (W8) |
|---|---|---|---|---|---|
| `yuyutsava.core.config` | 532 ms (860) | 2,398 | yes | < 100 ms, no | split up in W3: `llm.settings` 9 ms, 87 modules, no LangChain; `config.env` 1 ms |
| `yuyutsava.llm.providers` | 651 ms (1,314) | 2,987 | yes | — | 1 ms, 59 modules, no |
| `yuyutsava.memory.store` | 559 ms | 2,519 | yes | — | 50 ms, 333 modules, no |
| `yuyutsava.context.config` | 555 ms | 2,524 | yes | < 100 ms, no | 9 ms, 89 modules, no |
| `yuyutsava.storage.paths` | 7 ms | 94 | no | unchanged | 9 ms, 94 modules, no |

Slowest individual imports (`verify_imports.py --slowest`): `daemon.bootstrap`
1,703 ms, `daemon.main` 1,685 ms, `daemon.web.server` 1,415 ms,
`cli.commands.chat_repl` 1,395 ms, `llm` 1,393 ms.
After (W8): `daemon.bootstrap` 1,875 ms, `daemon.main` 1,846 ms,
`conversation.sessions` 1,628 ms, `cli.commands.chat` 1,627 ms,
`cli.commands.chat_repl` 1,618 ms — the entry points that build the agent still
load the framework, as they must; what changed is that nothing else does
(`llm` itself is 1 ms).

## Lint — ruff 0.16.5

Selected rules over `yuyutsava/`:

| Rule | Meaning | Count | After (W8) |
|---|---|---|---|
| `PLC0415` | import outside top level | 410 | 0 without a reason |
| `T201` | `print` | 259 | 207, all in `cli/` (0 outside) |
| `F401` | unused import | 121 | 0 (gated) |
| `I001` | unsorted imports | 84 | 0 under the project config (gated) |
| `PLR0913` | too many arguments | 77 | 84 (ratchet 28 under the project config) |
| `BLE001` | blind `except` | 67 | 3, the face watcher (gated elsewhere) |
| `B008` | function call in a default argument | 61 | 78 |
| `C901` | too complex | 57 | 29 (ratchet 4 under the project config) |
| `SIM105` | suppressible exception | 55 | 25 |
| `S110` | `try`/`except`/`pass` | 50 | 2, the face watcher |
| `F821` | undefined name | 35 | 0 (gated) |
| `PLR0912` / `PLR0915` | too many branches / statements | 24 / 22 | 9 / 1 |
| `B904` | `raise` without `from` in `except` | 13 | 0 (gated) |
| `ERA001` | commented-out code | 9 | 9 |
| `F841` / `F822` / `E741` / `F541` / `E402` | bug-class | 4 / 2 / 2 / 1 / 3 | 0 (gated) |

The *after* counts use `--isolated`, ignoring the project's per-file ignores
and thresholds, so they compare with the *count* column, which predates any
config. `B008` grew with the explicit dependency injection of W5
(`Depends(...)` defaults in the routers), which is FastAPI's idiom.

ruff's own `E402` finds 3 where the AST scan finds 31, because ruff exempts
imports that follow `os.environ` changes. The standard does not.

Over `test/` and `scripts/`: `PLC0415` 469, `T201` 82, `I001` 39, `E402` 15,
`F401` 13, `BLE001` 7, `S110` 2, `F841` 2.
After (W8): `PLC0415` 449, `T201` 110, `I001` 7 (isolated config), `E402` 0,
`F401` 0, `BLE001` 1, `S110` 1, `F841` 0. In-function imports in tests are
test-local by design and outside the ratchet.

`ruff format --check`: 360 of 563 Python files would be reformatted.
After (W8): all 764 formatted (gated).

## Tests and scripts

| Metric | Now | Target | After (W8) |
|---|---|---|---|
| Test files | 145 | — | 203 |
| Files using `unittest` / importing `pytest` | 115 / 2 | one runner | pytest runs all (199 are `unittest` style, 3 import pytest); 2,306 pass |
| `conftest.py` | 0 | 1 plus `test/support/` | 0: markers live in `pyproject.toml`, shared helpers in `test/support/` |
| `sys.path` manipulations in `test/` and `scripts/` | 10 | 0 | 1: `verify_framework_contract.py`, run as a file from anywhere |
| Private-name imports in tests | 16 | 0 | 0, guarded by `test/architecture/test_tests_use_public_names.py` |
| Fake classes duplicated across files | 7 names (up to ×4) | 0 | 0 duplicated; 5 generic names are reused by different fakes |
| Scripts documented in a README | 0 of 8 | all | 15 of 15 |
| Hermetic test subset run in CI | no | yes | yes (Python 3.11 and 3.12), plus a three-OS `platform` job |

## Electron app

| Metric | Now | Target | After (W8) |
|---|---|---|---|
| Lines (JS, JSX, CSS, HTML) | 15,822 | — | 20,192 (source, tests excluded; stylesheets and split-out modules) |
| Lint / format / type / test tooling | none | ESLint, Prettier, vitest | ESLint at 0 warnings, Prettier, 54 vitest tests, all in CI; no types |
| Components over 500 lines | 4 | 0 (≤ 300 each) | 0; 300-line cap, 9 files on a shrink-only allowlist |
| Largest file — `conversations/store.js` | 922 (one class, ~800) | ≤ 300 per module | five modules; `session.js` 636 (allowlisted) |
| `fetch` calls outside `src/renderer/api/` | 5 | 0 | 0 |
| Dead duplicate components | 1 | 0 | 0 |
| Most `style={{…}}` in one file | 40 (`ChatPanel.jsx`) | computed values only | 23 (`ActivityLog.jsx`); 650 → 512 app-wide, the worst five converted |

## Signals not used

`TODO`/`FIXME` markers (87) are **not** a hygiene signal here: the regex also
matches the product's "TODO board", which dominates the count.

## Gate baselines at `f4ec45c`

Recorded before any fix, and re-run after every wave.

| Gate | Result | After (W8) |
|---|---|---|
| `test/core/test_agent_fingerprint.py` | 13 tests OK | fingerprint and profiles: 21 passed (the fingerprint is now path-normalised and hermetic) |
| `test/core/test_agent_profiles.py` | 8 tests OK | (above) |
| `test/test_ports_is_a_leaf.py` | 8 tests OK | 8 passed (moved under `test/architecture/`) |
| `scripts/verify_framework_contract.py` | 8/8 contracts hold | 8/8 |
| `scripts/verify_loop_affinity.py` | 19 passed | 19 passed |
| `scripts/verify_imports.py` | 387 modules, 6 failures (`R-IMP-01`) | 478 modules, 0 failures |
| `yuyutsava --print-tools` output (sha256) | `08021171db6d…` | `08021171db6d…`, unchanged: the agent's tool surface is the same as at `f4ec45c` |
