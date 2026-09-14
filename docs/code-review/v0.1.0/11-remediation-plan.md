# 11 — Remediation plan

The waves of fixes for this review: what each changes, in what order, how it is
verified, and what is deliberately left for later. Work happens on the branch
`code-improvement`, cut from `main` at `f4ec45c`.

## Ground rules

- **No flag day.** The system is in daily use, so every commit leaves it working.
- **One concern per commit.** Mechanical commits (format, sort, move) never
  carry semantic changes. A behaviour change is its own `fix:` commit, with a
  test and a changelog line.
- **No compatibility shims.** Every importer is in this repository; a move
  updates its importers in the same commit.
- **Gates before changes.** Each wave starts with the check that proves it did
  no harm.
- **No billable calls in verification.** A real model-backed smoke run is offered
  to the owner with a cost estimate, never run unasked.
- **Stop rule.** Stop, split the change out and report when any of these happen:
  - the agent fingerprint changes unexpectedly;
  - a behaviour change slips into a refactor commit;
  - a wave's risk turns out larger than stated here.

## Order, and why

```
W0 safety net ─► W1 guardrails ─► W2 __init__ + imports ─► W3 layering + placement
                                                                   │
        W7 Electron ◄─ W6 tests ◄─ W5 errors, state, defects ◄─ W4 readability, SOLID, DRY
                                                                   │
                                                             W8 close-out
```

- **W2 comes before W3, and step W2.1 comes first within W2.** Most
  function-body imports that "break cycles" do so only because of the eager
  `core/__init__.py` (`R-INIT-01`). Moving them first would turn latent cycles
  into start-up crashes.
- **W3 comes before W4.** Decomposing a function that is about to move means
  doing the work twice.
- **Behaviour defects (W5) come after the structure is stable,** so each `fix:`
  lands in its final home with its own test.
- **The Electron app (W7) is independent** and could run in parallel; it is
  last only to keep one reviewer's attention on one stack at a time.

## Status

| Wave | Scope | Status |
|---|---|---|
| W0 | Safety net: `verify_imports.py`, `measure_code_health.py`, baselines | Done with this review |
| W1 | Guardrails | Done — see the outcome under W1 |
| W2 | `__init__.py` and import hygiene | In progress — steps 1–3 done; see the outcome under W2 |
| W3 | Layering and placement | Planned |
| W4 | Readability, SOLID, DRY | Planned |
| W5 | Errors, logging, state, defects | Planned |
| W6 | Tests and scripts | Planned |
| W7 | Electron app | Planned |
| W8 | Close-out | Planned |

---

## W0 — Safety net

**Delivers.**
- `scripts/verify_imports.py`, which imports every module first and alone.
  Negative-controlled: a cycle injected into `storage/paths.py` was reported
  as BROKEN and exited 1; the restored tree exited 0.
- `scripts/measure_code_health.py`, the source of every number in [02](02-metrics.md).
- Baselines for every gate, recorded in [02](02-metrics.md#gate-baselines-at-f4ec45c).

## W1 — Guardrails (no behaviour change)

| Commit | Change | Findings |
|---|---|---|
| `fix(types)` | Import every annotation name under `TYPE_CHECKING`; drop the dead `__all__` entries; fix the remaining `F841`/`E741`/`F541` | `R-IMP-04`, `R-IMP-05` |
| `build(lint)` | `[tool.ruff]` in `pyproject.toml`. Gate `F821,F822,F841,E741,F541,E402`. Allowlist, and ratchet down, `PLC0415`, `C901`, `PLR0913`, `BLE001`, `S110`, `T201`. A CI `lint` job. | STD-IMP, §6, §8 |
| `test(arch)` | `test/architecture/test_import_layers.py`: the layer map from STD-LAY-1 and the private-import rule, with an allowlist of today's violations that fails if a new edge appears **or** if a listed edge has disappeared without shrinking the list | `R-LAY-01`, `R-IMP-07` |
| `build(ui)` | ESLint flat config (`react`, `react-hooks`), Prettier, `npm run lint`, a CI step | `R-UI-01` |
| `style` | `ruff format` plus import sorting, alone, recorded in `.git-blame-ignore-revs` | `R-IMP-06` |

**Exit.** Every new gate is green and has been seen red on a deliberate break.

**Outcome — done.** On `code-improvement`:

| Commit | What landed |
|---|---|
| `4599d33` `fix(types)` | Every annotation name imported; all bug-class lint cleared (`R-IMP-04`, `R-IMP-05`) |
| `08cce31` `fix(test)` | The same rules cleared in `test/` |
| `99fa073` `build(lint)` | ruff gate at zero; `scripts/check_lint_ratchet.py` + baseline (BLE001 70, C901 20, PLC0415 432, PLR0913 38, S110 50, T201 86); CI `lint` job |
| `ac880ce` `test(arch)` | `test/architecture/test_import_layers.py`: 21 upward and 14 private imports allowlisted, both lists shrink-only |
| `a309b8a` `style` + `2dd8f89` | ruff format, 342 files: syntax trees, docstrings and `--print-tools` output unchanged; blame-ignored |
| `57b8fb5` `fix(ui)` | A hook-order bug in `ChatPanel.jsx`'s `Bubble`, found by `rules-of-hooks` |
| `cdd4eaa` `refactor(ui)` | Unused bindings removed, two JSX apostrophes escaped |
| `080f207` `build(ui)` | ESLint 9 and Prettier configuration; errors at zero, warnings capped at 33; CI Lint step |
| `994ee6c` `style(ui)` + `de2c54a` | Prettier, 86 files, render-equivalent in all 90 JS files; CI Formatting step; blame-ignored |
| `111fc6a` `fix(test)` | A regression from `a309b8a` (see below) |
| `ffca3f5` `build(lint)` | CI checks Python formatting, so the tree cannot drift back |

**A regression, caught and fixed.** The Python reformat broke one test:
`test/conversation/test_repair.py` searched `core/streaming.py` for the substring
`StreamEvent("notice"`, and the formatter wrapped both calls across lines (2
occurrences before, 0 after). Behaviour was unchanged; the reformat's syntax-tree
comparison and gate tests could not see a source-text assertion, and that file was
not in the set run before committing. Found during W2 step 1; every one of the 42
test files that read source text was then run, and no other broke. The test now
asserts on parsed calls, and is negative-controlled. Lesson carried into every
later mechanical change: run the source-reading tests, not just the gates.

Negative controls observed: the gate failed on an injected undefined name, the
ratchet on an injected `print`, the layer test on an injected `models → daemon`
import, and `verify_imports.py` on an injected cycle; each passed again once the
file was restored.

**Deviation.** Import sorting (`I001`) moved from W1 to W2. Reordering imports
changes module initialisation order, and six modules on this tree crash depending
on that order (`R-IMP-01`); it is safe only after W2.1 empties `core/__init__.py`.

## W2 — `__init__.py` and import hygiene

1. **`core/__init__.py` becomes a docstring;** `test/test_async.py:35` imports submodules.
   Exit: `import yuyutsava.core.config` under 100 ms without LangChain;
   `verify_imports.py` fixes `skills.registry` and `tools.search`; fingerprint
   byte-identical. (`R-INIT-01`)
2. **Every other `__init__.py` meets STD-PKG:**
   - slim or docstring-only initialisers;
   - `llm/providers/registry.py`;
   - docstrings for the five packages without one;
   - accurate package docstrings.

   (`R-INIT-02`…`06`)
3. **`yuyutsava/config/env.py`** — `env`, `env_flag`, `env_int`, `env_float`. Every
   hand-written parser migrates, keeping its current accepted values, and the
   private `_env` goes away. (`R-DRY-01`, `R-IMP-07`)
4. **Function-body imports move to the top, one package per commit:**
   `core/engine.py`, `context/`, `policy/adapter.py`, `cli/`, `agents/`,
   `daemon/bootstrap.py`, `platform/elevation.py`, `io/wake.py`,
   `core/tracing.py`, `skills/`, `llm/providers/azure.py`. Genuine optional or
   heavy dependencies keep `# noqa: PLC0415  <reason>`. (`R-IMP-02`)
5. **Mid-file imports:**
   - `register_builtin_blocks()` for the TODO board;
   - the session-store accessor moves beside its factory;
   - the gRPC environment setting is resolved as described in `R-IMP-03`.

   (`R-IMP-03`, `R-IMP-01`)
6. **Private names become public;** third-party private APIs are replaced. (`R-IMP-07`, `R-IMP-08`)
7. **Ratchets tightened.**

**Exit.**
- `verify_imports.py`: 0 failures.
- Deferred first-party import ratio < 8%.
- `mid_file_imports` 0.
- `private_cross_package_imports` 0.
- `inits_*` metrics 0.
- Fingerprint byte-identical.

**Outcome so far — steps 1–3 done.** On `code-improvement`:

| Step | Commit | What landed |
|---|---|---|
| 1 | `2ce0c1c` | `core/__init__.py` is a docstring. `import yuyutsava.core.config`: 532 ms → 12–18 ms, 2,398 → 107 modules, no LangChain (`R-INIT-01`) |
| 2 | `b0b73e3` | The `context`, `memory`, `agents` and `mcp` inits stop importing (`R-INIT-02`) |
| 2 | `c4a7475`, `c2ebd89` | Ten inits become docstring-only; the rest re-export only what is imported from them (`R-INIT-03`, part of `R-INIT-06`) |
| 2 | `6e83f16` | The provider registry moves to `llm/providers/registry.py` (`R-INIT-04`) |
| 2 | `b7478e2`, `12bf744`, `8863a0f` | The last five init docstrings; `ports` stops re-exporting two unused protocols; four docstrings corrected (`R-INIT-05`, `R-INIT-03`, `R-INIT-06`) |
| 2 | `50b65f9` | `measure_code_health.py` now counts attribute reads through a bound package (`yplatform.kill_tree`) as uses. It had reported three live `platform` exports as unused |
| 3 | `21db0c8` | `yuyutsava/config/env.py`: `env`, `env_int`, `env_float`, `env_flag`, with tests |
| 3 | `2eff394` | The private `core.config._env` becomes the public `env`; six private cross-package imports go (`R-IMP-07`) |
| 3 | `b239429` | 42 numeric settings in 12 modules use `env_int` / `env_float` |
| 3 | `fc7dbde` | 12 flags that already stripped their value use `env_flag` |
| 3 | `dbd8e30` | **fix:** 8 flags that did not strip now do, so a padded `"1 "` counts. A behaviour change, so it is its own commit (`R-DRY-01`) |

**Measured at `8863a0f`, against the `f4ec45c` baseline.**
- Init metrics:
  - `inits_without_docstring` 5 → 0;
  - `inits_with_logic` 1 → 0;
  - `inits_with_unused_exports` 24 → 0, with the corrected metric.
- `private_cross_package_imports` 14 → 8.
- `history_narration_phrases` 176 → 166.
- `in_function_imports` 415, unchanged. Moving these imports is step 4.
- `deferred_internal_import_ratio` 0.246 → 0.251. Step 4 is the step that lowers it.
- `mid_file_imports` 5, unchanged (step 5).

**How the migrations were proven.**
- **Behaviour probes.** Each probe compared the tree before and after, in fresh interpreters confirmed to import their own tree:
  - numeric settings: every site under 23 environments, identical;
  - flags: 35 environments, identical for the refactor, and exactly the 31 intended differences for the `fix:` commit.
- **After every commit:** the W1 gates, `yuyutsava --print-tools` (sha256 unchanged), and a sweep of 42–76 test files. Every failing file was re-run at `f4ec45c` and failed there identically.

**Left out of step 3 on purpose.**
- Sites the helpers cannot express:
  - `LANGFUSE_ENABLED`: unset and empty differ;
  - `FASTER_WHISPER_VAD_FILTER`: empty means off;
  - `YUYUTSAVA_ALLOW_BLOCKING`: three states;
  - `first_chunk_retry._max_retries`: a blank value and garbage fall back to different defaults.
- `WAKE_THRESHOLD` and the sessions busy timeout raise on a bad value. Making them forgiving is a W5 behaviour change.
- Enum-like selectors (`STT_PROVIDER`, `YUYUTSAVA_STORAGE_BACKEND`, …) are not flags.

**Found along the way.**
- `R-TST-08`, `R-TST-09`, `R-TST-10`.
- New `R-TST-01` evidence: pytest is not declared, so the two pytest files cannot be imported.

**Still open in W2.** Steps 4–7, and every exit criterion except the init metrics:
- `verify_imports.py` still fails the three `todoboard.block_*` modules (step 5);
- the deferred ratio;
- mid-file imports;
- private imports.

## W3 — Layering and placement

1. **Remove the nine two-way dependencies** (`R-LAY-01`, `R-LAY-02`):
   - LLM settings → `llm/settings.py`; `EventsConfig` → `events/config.py`;
     `DaemonConfig` → `daemon/config.py`; `core/config.py` removed.
   - Budget and usage policies → `policy/builtin/`; the usage store → `storage/`.
   - `AskPrompt` and the channel-router protocol → `ports/channels.py`; a decision port for `channels/`.
   - Public `build_retrieval_stores` in `storage/factory.py`.
   - Search tools and their exceptions → `discovery/`.
2. **Empty `core/` of what does not assemble agents** (`R-PLC-01`…`04`, `R-PLC-10`):
   - `policy/builtin/` for the six policies; `docker_sandbox/backend.py`;
     `llm/pricing.py`; `discovery/tool_registry.py`;
   - `yuyutsava/text.py`; CLI tooling → `cli/`;
   - `core/streaming.py` split into driver, terminal prompts and an event registry;
   - renames `command_safety.py` and `PolicyKind`.
3. **Other moves** (`R-PLC-05`…`09`, `R-PLC-12`):
   - `io/` + `audio_io/` → `voice/`;
   - the tool placement rule, removing `tools/`; `agents/db_tools` → `storage/introspect_tools.py`;
   - `sessions/runner.py` helpers → `conversation/`;
   - router schemas → `schemas/`; voice blobs → `storage/`;
   - `visuals/latex.py`, `visuals/code_image.py`.
4. **Delete the dead code** in `R-PLC-11`.

**Exit.**
- Layer allowlist without two-way dependencies; no cycle through `core`, `llm` or `storage`.
- `core/` holds agent assembly only.
- `verify_imports.py`, fingerprint and framework contract green after every commit.

## W4 — Readability, SOLID, DRY

Characterization tests first, for every unit that is split.

- **`converse`** → `daemon/web/services/converse_session.py`, one handler per frame. (`R-RDB-01`)
- **`run_chat_repl`** → `ChatOptions`, a slash-command registry that generates
  `/help`, a `ReplLoop` class; printers → `cli/render/`. (`R-RDB-02`, `R-SOL-02`)
- **Tool factories:**
  - `bind_tools` → `TaskRunnerTools`;
  - `make_context_tools` and `make_todo_tools` → one factory per tool family.
- **Composition roots:** `build_daemon` and `_async_main` → builders of at most
  150 lines; `_build_parser` → subparsers. (`R-RDB-02`, `R-KIS-01`)
- **`MasterDeps` and `AppDeps`** typed with `ports`; the orchestrator uses the profile for budget
  and usage; the docker/local branch is built once. (`R-RDB-03`, `R-SOL-04`, `R-KIS-02`)
- **Renderers:** a `ChatRendererProtocol`; `PayloadRing`; `tool_format` as the only
  copy of its helpers; the zone chip uses `zones.classify_zone`; a test that
  every renderer handles every event kind; the dashboard split. (`R-SOL-01`, `R-SOL-03`, `R-DRY-04`)
- **God classes:**
  - `TodoExchange` becomes a facade over services;
  - `AsyncTaskHealthWatcher` is split into parts;
  - `context/meter.py` is split into modules.

  (`R-SOL-01`)
- **Registries** for providers, visual kinds and tool events. (`R-SOL-02`)
- **Shared helpers:**
  - `flatten_content(sep=)` and one thread-id helper;
  - voice WAV helpers and the sample-rate constant;
  - `palette.py` and `humanize_duration`;
  - `prompt_for_ask`, `classify_provider_busy`, `build_async_host`.

  (`R-DRY-02`…`06`)
- **Docstrings:** correct every contradiction; remove history narration. (`R-DOC-01`, `R-DOC-02`)
- **Naming and constants.** (`R-RDB-04`, `R-RDB-05`, `R-RDB-06`, `R-KIS-03`)

**Exit.**
- 0 functions over 200 lines; ≤ 10 over 100 lines; ≤ 3 with more than 10 parameters.
- Highest complexity ≤ 25.
- 0 classes over 400 lines.
- `history_narration_phrases` 0 after review.
- Fingerprint byte-identical.

## W5 — Errors, logging, state, defects

- **Classify the 58 silent handlers** as boundary (log plus reason) or internal (narrow type). (`R-ERR-01`, `R-ERR-02`)
- **One exception type for missing voice packages;** `raise … from`. (`R-ERR-03`)
- **`print` → logging** outside `cli/`. (`R-LOG-01`)
- **Retire the seven singletons through injection;** fix the hidden channels. (`R-GLB-01`, `R-GLB-02`)
- **One `fix:` commit each,** with a test:
  - `R-BUG-01` context budgets;
  - `R-BUG-02` price cache;
  - `R-BUG-03` plain renderer visuals;
  - `R-BUG-04` attach port;
  - `R-BUG-05` `/clear` in the dashboard, reproduced first;
  - `R-BUG-06` one-shot turns through `ConversationService`.

**Exit.**
- `silent_broad_excepts` 0; `prints_outside_cli` 0.
- `set_default_definitions` 0; `default_accessor_call_sites` 0.
- Each `fix:` has a test that failed before it.

## W6 — Tests and scripts

- **Runner:** pytest as the runner of record; markers `pg`, `billable`, `slow`;
  a CI job for the hermetic subset, negative-controlled. (`R-TST-01`)
- **Layout:**
  - `test/conftest.py` and `test/support/`; no `sys.path` changes;
  - the tree mirrors the package;
  - `test/README.md`.

  (`R-TST-02`, `R-TST-03`, `R-TST-05`)
- **Tests use the public API.** (`R-TST-04`)
- **New tests** for units extracted in W3–W5, and contract tests for `platform/`. (`R-TST-06`)
- **`scripts/README.md`.** (`R-SCR-01`)

## W7 — Electron app

- **Dead code and consistency:**
  - delete the dead `AskCard`;
  - rename `useAsks.js`;
  - route every `fetch` through `api/`;
  - one `src/main/config.js`.

  (`R-UI-04`, `R-UI-05`, `R-UI-07`, `R-UI-08`)
- **The conversation store:** vitest tests first, then the split into reducer, transport and
  lifecycle. (`R-UI-02`)
- **Components** of at most 300 lines; theme classes instead of static inline styles. (`R-UI-03`, `R-UI-06`)
- **ESLint green; vitest in CI.** (`R-UI-01`)

**Verification.** `npm run lint`, `npm run build`, `npm test`, then drive the
**Electron app** itself (not a browser): chat, the Artifacts gallery (cards,
RUN/SOURCE, expand), the TODO board and ThinkBoard, voice overlay,
Settings → Usage, asks.

## W8 — Close-out

- **Re-measure.** Re-run [02](02-metrics.md) and fill in the *after* column;
  update the scorecard in [00](00-executive-summary.md) and every finding's status.
- **Retire the old review:**
  - finalize [10](10-legacy-ledger.md) and move ADR-001…004 to
    `docs/architecture/decisions/`;
  - repoint every reference to `docs/architecture/review/`
    (`README.md`, `CONTRIBUTING.md`, `docs/README.md`, `docs/architecture/`,
    `pyproject.toml`, `scripts/verify_framework_contract.py`, tests and source
    docstrings);
  - delete the directory.
- **`CONTRIBUTING.md`:** conventions point to [01](01-standards.md); the known
  gaps drop "no lint gate"; the test instructions show the runner and the
  hermetic subset.

---

## Carried

Deliberately not fixed in this remediation, each with its reason:

| Item | Why it waits | Next step |
|---|---|---|
| **`usage` storage domain** (F-D02) | Its SQLite and Postgres implementations disagree on `task_id` handling; unifying them changes recorded data, so the owner decides the semantics. | Decide the semantics; then migrate with the existing playbook. |
| **Schema centralization** (F-D04) — `CREATE TABLE` in 16 modules | Moving schema ownership needs a migration-version plan and a live-Postgres verification pass of its own. | A dedicated plan after W8. |
| **Constructing half of F-T03** | A second agent constructor is a strategic choice, not a code-quality fix; a test pins the gap. | Revisit when a second framework is actually needed. |
| **C3** — interrupt/resume tripwire | Needs a scripted interrupt round-trip against the real graph runtime. | Add alongside the next change to the ask/resume path. |
| **Renaming `yuyutsava/platform`, `yuyutsava/mcp`** (`R-PLC-12`) | `platform` has 16 package-root importers and carries OS invariance; `mcp` is contained by `test/mcp_layer`. The benefit is small next to the churn. | One rename-only change each, if ever. |
