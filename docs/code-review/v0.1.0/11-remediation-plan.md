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
| W2 | `__init__.py` and import hygiene | Done — see the outcome under W2 |
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

**Outcome — done.** On `code-improvement`:

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
| 4 | `4b2e046`, `7e45d68`, `832d2d5`, `6593905`, `31910a7`, `536c0ec`, `600a16e` | Function-body imports move to the top, one package group per commit: engine, context and policy, cli, agents, daemon, storage and domains, the rest. What stays carries `noqa: PLC0415` with a measured reason (optional extra, heavy, platform-specific, upward until W3, import cycle until W3, subprocess entry) (`R-IMP-02`) |
| 5 | `890e28d` | The TODO board's registry core moves to `todoboard/block_registry.py`; `artifacts.py` imports the block modules at its top. The three `todoboard.block_*` modules import on their own (`R-IMP-03`, `R-IMP-01`) |
| 5 | `2a76e1e` | No imports after code: the session-store imports and the interrupt formatters join their headers; the gRPC setting moves below the entry points' imports, guarded by `test/cli/test_grpc_env_order.py` (`R-IMP-03`) |
| 6 | `72427f9` | The eight private cross-package names become public; the private-import allowlist is empty (`R-IMP-07`) |
| 6 | `1e62c6c` | The langgraph-api version notice uses public APIs; its support rule agrees with langgraph-api's in 2,880 cases (`R-IMP-08`) |
| 7 | `85363f6` | The last function-level imports without a reason get one; `platform/elevation.py`'s stdlib imports move to the top |
| 7 | `e09c27c` | 133 unused imports removed; `F401` gated (`R-IMP-06`) |
| 7 | `1cc5223`, `e643344` | Every import block sorted; `I001` gated; blame-ignored (`R-IMP-06`) |
| 7 | `ae7af6f`, `e307480` | A `noqa` with a false reason removed; `llm` stops re-exporting `ModelHandle`, which nothing imports from the root any more (`R-INIT-03`) |

**Measured at `e307480`, against the `f4ec45c` baseline.**
- `verify_imports.py`: 6 failures → 0 (392 modules).
- `in_function_imports` 415 → 96; `deferred_internal_import_ratio` 0.246 → 0.046 (target < 0.08).
- `mid_file_imports` 5 → 0; `imports_after_code` 31 → 2, both documented.
- `private_cross_package_imports` 14 → 0.
- `inits_without_docstring`, `inits_with_logic`, `inits_with_unused_exports`: 0.
- ruff: `F401` 134 → 0 and `I001` 147 → 0, both now in the gate; `PLC0415` ratchet 432 → 22, all 22 in `scripts/` (W6).
- `history_narration_phrases` 176 → 164.
- Agent fingerprint byte-identical; `yuyutsava --print-tools` unchanged throughout.

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

**How steps 4–7 were proven.** After every commit: the gates, `verify_imports.py`,
`--print-tools`, a sweep of the test files that import what changed (every failure
re-run at `f4ec45c`), and the REPL and daemon smoke tests; per-file syntax-tree
proofs for the moves and the sort; before/after probes for the block registry and
the version notice. The sort was checked against the whole suite, and the 29
Postgres-backed files were run with Postgres up and no case skipped.

**Caught before committing.**
- The import-moving script's cycle check missed `from pkg import submodule` and
  edges through a package `__init__`; its first run on the last group broke 13
  modules. It was reverted, and the script now also imports each module and its
  moved targets in a fresh interpreter before keeping a move.
- The unused-import fix removed a re-export that `memory/store_unified.py` read
  through a multi-line import; `verify_imports.py` reported 10 broken modules.
- During the full-suite sweep another project's Postgres held port 5435 and
  rejected our login, so 21 storage files errored instead of skipping. Not a
  code change; the files were re-run once YUYUTSAVA's own database was back.

**Carried out of W2.**
- 47 first-party function-body imports that would be upward or close a cycle today;
  W3 removes their cause.
- The 22 unmarked `PLC0415` imports in `scripts/`, and the `E402` exemptions for
  tests that edit `sys.path` (W6, `R-TST-02`).

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

**Outcome — done.** On `code-improvement`:

- **The cycle is gone.** `subpackage_two_cycles` 18 → **0**, and the strongly
  connected component that spanned 21 of 31 sub-packages is **1** package.
  `private_cross_package_imports` **0**; `ALLOWED_UPWARD` holds one entry,
  `("artifacts", "todoboard")`, carried to W4. Deferred internal imports
  **2.4%**, `mid_file_imports` **0**.
- **`core/` is agent assembly only** — twelve modules. Config, pricing, the six
  policies, the tool registry, prompts, tracing, text helpers, the docker
  backend and the CLI tooling all left; `streaming.py` kept the driver and the
  daemon sink while the terminal sink became `cli/render/terminal_stream.py`.
  `core/streaming.py` now holds no `print` and imports neither `sys` nor
  `asyncio`.
- **Packages merged or removed:** `io/` + `audio_io/` → `voice/` (+ `backends/`),
  `sessions/` → `conversation/sessions.py` + `cli/session_run.py`, `tools/` →
  `websearch/`, `agents/db_tools/` → `storage/introspect_tools.py`. Thirteen
  router models moved to `daemon/web/schemas/`.
- **Renames:** the profile enum is `PolicyKind` (the `Policy` ABC keeps its
  name), `visuals/latex.py` and `visuals/code_image.py` no longer shadow the
  stdlib, `permission_middleware.py` is `command_safety.py`.
- **Dead code:** three modules and nine definitions deleted. Two entries in
  `R-PLC-11`'s table were **not** dead and are kept with reasons
  (`needs_repair`, `llm.factory.supports` — both test-only), and `Capability`
  was live, so only the free function beside it went.
- **Ratchets:** `T201` 86 → **41** (prints moved into `cli/`, which is exempt by
  design; one genuine print surfaced below `cli/` and is carried to W5),
  `BLE001` 68 → 67, `PLR0913` 38 → 37. `PLC0415` steady at 22.

**Departures from the task list, deliberate:**

- *"Search tools and their exceptions → `discovery/`"* — not done. `discovery/`
  is progressive disclosure over the agent's own catalog; these query the web
  through Tavily/Exa. They share only the word "search", so web search became
  its own domain (`websearch/`) instead.
- *"Public `build_retrieval_stores` in `storage/factory.py`"* — the factory is
  `core/stores.py`; `storage/factory.py` no longer exists. STD-PLC-1 in
  01-standards.md was pointing at that dead path and now names the real one.
- `tracing.py` went to `llm/` rather than staying in `core/`, and `AppContext`
  plus the `artifacts`→`todoboard` edge are carried to W4/W5.
- `R-PLC-10` stays **open**: `llm/pricing.py` exists, but live lookup still
  sniffs `base_url` instead of asking a provider — the half that mattered.

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

**Progress — in flight.** Measured with `measure_code_health.py`, not estimated.

| Metric | Review | Now | Exit |
|---|---|---|---|
| functions > 200 lines | 11 | **8** | 0 |
| functions > 100 lines | — | **39** | ≤ 10 |
| functions > 10 params | 14 | **8** | ≤ 3 |
| max complexity (approx) | ~130 | **38** | ≤ 25 |
| classes > 400 lines | 11 | **8** | 0 |
| `C901` (ruff, the gate) | 19 | **13** | allowlist only shrinks |
| `PLR0913` | 38 | **31** | allowlist only shrinks |
| `BLE001` | 68 | **62** | W5 |
| `S110` | 50 | **42** | W5 |
| history narration | 176 | **160** | 0 |

Done so far:

- **`converse`** 771 → 105 lines: the router accepts the socket and delegates;
  `converse_session.py` holds the frame types and `converse_connection.py` the
  connection. (`R-RDB-01`)
- **Tool factories** are packages, one module per family: `bind_tools` 609 →
  largest 137, `make_todo_tools` 266 → largest 104, `make_context_tools` 242 →
  four surfaces preserved.
- **`MasterDeps`** and **`AppDeps`** replace the 33-, 32- and 25-argument
  parameter lists; `daemon/web/server.py` and its 25-argument pure forwarder
  `make_app` are deleted. (`R-RDB-03`)
- **`TodoExchange`** is a facade over four services — see `R-SOL-01`.
- **One CLI subcommand registry.** The commands were defined twice, in
  `_build_parser` and in `main`'s dispatch, so `/help` and the parser could
  disagree — and did: four commands were missing from one of them
  (`R-BUG-12`). `SUBCOMMANDS` is now the single definition and generates both.
- **One terminal palette** (`yuyutsava/ansi.py`): nine escape codes written out
  157 times across ten modules. A leaf, because the daemon's terminal channel
  writes them too. (`R-DRY-04`)
- **One content flattener** (`R-DRY-02`), and the plain renderer draws `image`
  events (`R-BUG-03`).
- **The two renderers are peers.** `RichChatRenderer` subclassed `ChatRenderer`
  while overriding all seven of its public methods, so it inherited no
  behaviour and twenty-two private helpers, fifteen of which it had replaced
  and could not reach — the coupling `R-BUG-03` came out of. They now share
  `render/ring.py` (`PayloadRing`), `render/zones.py` and `tool_format` by
  composition, behind `render/protocol.py`. `ChatRenderer` 501 → 375 lines, so
  `classes_over_400` is 9.
- **The REPL's reports left the REPL**: `render/startup.py` (banner, status
  line, version notice), `render/reports.py` (`/help`, `/usage`, `/skills`,
  `/context`, the permission card) and `cli/usage.py` (the `llm_usage` read).
  `chat_repl.py` 1,486 → 1,137 lines, and three tests stopped importing
  private names from it.

- **The meter is a package**, one stage per module in the order a reading is
  built — `snapshot` → `bus` → `sizing` → `calibration` → `policy`. 1,034 lines
  and eight module-level stores became six modules; the import path and all 15
  exported names are unchanged.
- **Three pieces left `ChatDashboard`**: `StdioCapture`, `ScrollView` and
  `InputBroker`, 607 → 438 lines with 40 tests that need no application. It
  stops above 400 deliberately — see `R-SOL-01`.

- **The watcher is a package that routes.** The largest class in the tree, 724
  lines and thirteen jobs, split between observing (`TaskPoller.classify`
  returns a verdict) and acting (`InterruptHandler`, `TerminalReporter`), with
  the watcher deciding which. Verified with background subagents actually
  switched on: "async watcher: running", ten poll cycles, no faults.

Still open in this wave: `build_daemon` (451), the agent builders in
`core/engine.py` and `core/agent_stack.py` (297, 281 with 19 parameters, 239,
211), `_async_main` (224), `_build_parser` (205 — the duplicate definition is
fixed, the length is not), `run_chat_repl` itself (261) and the rest of its
module, the the rest of the `chat_repl` module, the remaining DRY helpers, the provider and
visual-kind registries, and the 160 history-narration phrases.

Eight classes remain over 400 lines, none of them named by `R-SOL-01`:
`DockerSandboxBackend` (490), `UnifiedTodoStore` (489), `ConversationService`
(468), `TaskRunnerAgent` (453), `ChatDashboard` (438, a stated stop),
`TelegramChannelPlugin` (420), `ConverseConnection` (417) and
`RichChatRenderer` (407).

**Two departures from the task list, deliberate.**

- *"The zone chip uses `zones.classify_zone`"* — not done, because that is the
  wrong function. `agents/task_runner/zones.classify_zone` is the **policy**
  classifier: it canonicalizes the path, requires a sandbox root and can answer
  `SYSTEM_CRITICAL`, because its verdict decides whether an operation runs. The
  chip labels the argument as the agent wrote it and shows nothing when it
  cannot tell. Using the policy one would change what the chip says and make a
  display concern depend on a sandbox root. Both renderers now share the
  display classifier instead, so the two chips cannot disagree with each other.
- *"The plain renderer drops its helper copies in favour of `tool_format`"* —
  done for `looks_like_error` and `error_message`, which were proved identical
  over 21 inputs, and **not** for `compact_summary`, whose `tool_format`
  version routes through `sanitize`/`truncate` and differs by a character on
  long values. Substituting it would change what the terse tool line prints.

**Bugs found while doing this wave, each fixed in its own commit:** `R-BUG-03`
(plain renderer dropped visuals), `R-BUG-08` (branch mismatch), `R-BUG-09` (an
ask race that hung production), `R-BUG-10` (a rename broke every MCP server),
`R-BUG-11` (whitespace transcript), `R-BUG-12` (`/help` hid four commands), and
`R-TST-15` (the fingerprint gate could not fire). Four of those were found by
live testing that the static gates had passed — which is why every commit in
this wave now ends with a daemon restart and a real turn.

**Audit against the plan — 2026-09-24.** Asked whether this wave was
following the plan, the honest answer was *in direction, not in sequence*.

*Verification.* Three per-commit or per-wave checks had not been run all wave.
All three were run and pass: `verify_framework_contract.py` (8 of 8 contracts,
including the general-purpose override that the `_subagent_spec` and
`search_tools` changes could have broken), `verify_loop_affinity.py` (16
passed; the Postgres checks skip because the database container is stopped),
and `--print-tools`. `verify_imports --compare` had no baseline to compare
against, but it compares import *success* only, and with no module failing,
the plain run already catches that. Import *time* was never gated after W2;
measured by hand, median of five fresh interpreters against the end of W3, the
entry points are about 4% slower, and all 43 extra modules are first-party —
the per-file cost of splitting god-classes into packages, not a dependency
creeping onto the startup path.

*Sequence.* Work followed the review's findings more than this wave's task
list. `R-DRY-05` was worked through all nine of its sub-items, though this list
names two of them; three of the nine turned out to be defects, so that time
was not wasted, but while it was spent these named items stayed untouched: WAV
I/O and the sample-rate constant, one duration humanizer,
`classify_provider_busy`, the visual-kind and tool→event registries, the
provider alias→settings registry (the *builder* dispatch is a registry since
W2; `llm_settings_from_env` is still a ten-branch `if` chain), and the
docker/local branch.

*Two departures that were not recorded when they were made:*

- **No `ReplLoop` class.** `run_chat_repl` went 504 → 261 lines behind
  `ReplCommands` and an `Outcome` enum instead. It is still 261 lines, so the
  task this bullet was for is not finished.
- **No `TaskRunnerTools` class.** `bind_tools` became a package with one module
  per family, which is what the class was for, but as functions.

*Exit criteria.* Only one of six is met: the fingerprint is byte-identical.
Functions over 200 lines 8 (target 0), over 100 lines 38 (≤ 10), over ten
parameters 8 (≤ 3), highest approximate complexity 38 (≤ 25), classes over 400
lines 8 (0), history narration 159 (0).

**Back in plan order after the audit.** The three remaining DRY bullets are
done, and two of them were defects:

- **WAV I/O and the sample-rate constant** (`bfc7b55`): `yuyutsava/wav.py`
  writes, `voice/pcm.py` holds the 16 kHz speech format. The writer's rate is
  required — a TTS engine writes at its own rate and a wrong one raises
  nothing, it only plays back at the wrong pitch. Verified with a real engine:
  `say` at 22,050 Hz stays 22,050 Hz.
- **One duration humanizer** (`a36cdc7`): `text.humanize_duration`. The
  sessions table's "2h ago" and `daemon status` keep their own shapes; they
  answer different questions.
- **`classify_provider_busy`** (`eca8afb`) — **a defect.** Three surfaces
  guessed the status from the class name. The retry predicate also retries
  `DeadlineExceeded` and `Aborted` (504 and 409), so a timeout was announced
  as "provider busy (503)", and the guesses disagreed with each other about
  subclasses. It now reads the status the SDK attaches.

Then two of the three OCP registries:

- **Provider alias→settings** (`da8eadd`): `llm/settings.SETTINGS_BY_PROVIDER`
  replaces the twelve-branch chain in `llm_settings_from_env`, and the
  "unknown provider" error lists names derived from the table, so it can no
  longer offer a provider that does not exist or omit one that does.
- **Visual kinds** (`1031bab`): `visuals/render.RENDERERS` maps each kind to
  the module and function that draw it, and `KINDS` is derived from it —
  before, a kind in `KINDS` without a branch in `_load` failed with
  "'NoneType' object is not callable". Renderers are still imported only on
  first use; a test proves building the tools does not load matplotlib. The
  `vis_*` tools stay one per kind: each has its own argument schema the model
  reads, so folding them into the table would change the agent's tool surface
  (and the fingerprint) for no gain. A new kind is one row plus its tool.
  Verified live: all six kinds rendered to PNG through `POST /visuals/render`
  on a restarted daemon, and an unknown kind got a 400 naming the six.

The third, **tool→event**, was already a registry: `ports/tool_events` since
`1525f2c` (W3), with the rules beside their tools (`visuals/events.IMAGE_EVENT`,
`artifacts/events.ARTIFACT_EVENT`). One consumer had kept its own copy of the
artifact tool names — the history replay in `daemon/web/routers/sessions.py` —
so a tool added to the rule would have shown its artifact live and lost it on
reload. It now asks `ARTIFACT_EVENT`. Nothing tested the replay; four
characterization tests came first (`test/web/test_session_history_artifacts.py`).
Verified live: 189 real session histories are byte-identical before and after,
and in an isolated daemon a real turn's `artifact_create` came back attached to
its reply on reload, with the id the live turn streamed.

The **docker/local branch** is built once (`R-KIS-02`): characterization
first, since the fingerprint never built docker mode.

The rest of `R-KIS-02` except the policy items: TTS written once
(`8d26cce`), and the REPL parses a command line once (`CommandLine`), with
behavioural dispatch tests first. Verified live in a piped REPL: every
command answered as before, and a real turn replied.

**`run_chat_repl` 261 → 108 lines**, with the `ReplLoop` the plan named: the
loop (read, dispatch, turn, cost line) is a class with its own tests
(`test/cli/test_repl_loop.py`), and the input reader, session start, retry
listener and teardown are functions beside it. A deterministic piped REPL
session was byte-identical before and after; real turns across `/new`
behaved the same on the old and new code, and the session rows confirm the
rotation (the post-`/new` session holds only its own turn). One observation
for W5, not caused by this change: once in four runs the first turn printed
no cost line; not reproduced, and the usage row is written inline in
`after_model`, so the cause is not yet known.

**Docstring sweep** (`R-DOC-01`, `R-DOC-02`): history narration 159 → 2
(both in agent prompts, reviewed), and every contradiction still present
fixed. 106 files, comments and docstrings only — proven by comparing each
file's AST, docstrings stripped, with the previous commit.

Still open in this wave, both waiting on a decision: the two `R-KIS-02`
policy items, and `build_daemon`/`_async_main` (`BootContext`).

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
  - `R-BUG-06` one-shot turns through `ConversationService`;
  - `R-BUG-07` a daemon started from the app serves its API.
  - `R-BUG-08` an answered inline ask resumes the turn — **landed early, in W4**
    (`7babeca`): it blocked W4's own characterization tests, so it could not wait.
  - `R-BUG-09` an answer that beats its waiter is held — **landed early, in W4**
    (`5873e00`): found by live-testing `R-BUG-08`'s fix against the running
    daemon, where a promptly answered permission prompt hung the turn outright.

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

### Closing out the standing failures

The suite settles at **21 failures, and they are five root causes, not
twenty-one problems.** Triaged 2026-09-19 by running each group alone:

| group | failures | cause | kind |
|---|---|---|---|
| `memory/test_embedder.py` | 6 | injects `emb._client`, but the loop-affinity work replaced it with `_clients: LoopLocal[AsyncClient]`. The fake is never used, so the test quietly calls the **real** embed endpoint — which is why it "varies with local Ollama". | test debt (`R-TST-08`) |
| `daemon/test_build_retrieval.py` | 4 | calls `build_retrieval()` without the `workspace` keyword it has since gained | test debt (`R-TST-09`) |
| `web/test_todo_board_dependency.py` (8 subtests), `storage/test_app_context.py`, `storage/test_feedback_purge.py` | 10 | `test_build_storage` calls `set_default_session_store(...)`, caching a module-level `_DEFAULT_STORE` that restoring the environment cannot un-cache | **product**, not test (`R-TST-13`) |
| `web/test_converse_ws.py::test_inline_ask_roundtrip` | 1 | asserts the old `ask["payload"]` frame shape, across three stale layers | test debt (`R-TST-11`) |

All ten in the third group **pass in isolation** (19 passed, 14 subtests) and
fail only in a shared process. That group is therefore not fixed here: the
global singleton *is* the defect, and W5 already owns retiring the seven
`set_default_*` accessors through injection. Fix the cause, and the ten
disappear; patch the tests, and the daemon keeps its hidden global.

The other eleven are genuinely stale tests, and each is restored to asserting
the behaviour it was written for — the char cap, the nomic prefixes and the
dimension check are all still real rules, the tests simply stopped reaching
them.

**Treat each as a suspect, not as known debt.** `R-BUG-10` was filed as exactly
this kind of "obviously stale" symptom and turned out to be every MCP server
failing to load. A failing test is evidence about the product until proven
otherwise, so each fix states which it was.

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
