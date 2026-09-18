# 08 — Tests and scripts

How the test suite is organised and run, and the state of maintainer scripts.
Rules: [01 §12–13](01-standards.md). Numbers: [02](02-metrics.md).

**Headline.** The tests that matter most are excellent: the agent fingerprint,
the framework-contract tripwires and the storage parity suites were all built
and negative-controlled on purpose. The suite around them has no shape. There
are two runners and no configuration, so nothing can mark which tests need
Postgres or an API key, and CI therefore runs no tests at all.

---

## R-TST-01 — No runner configuration, no markers, no CI test gate

**Severity** P1 · **Wave** W6 · **Status** Runner landed in W4; markers and CI open

**Claim.** The suite cannot be run safely by a machine.

**Evidence.**
- 115 files use `unittest`; 2 import `pytest`. There is no `conftest.py` and no
  `[tool.pytest.ini_options]`.
- `pytest` was neither declared in `pyproject.toml` nor installed in the project
  venv (W4 declared it under the `dev` extra and installed it).
- This bullet undercounted the damage, and W4 corrected it: only
  `test/web/test_voice_ws.py` fails at `import pytest`. **Five** files are
  written in pytest style, and in the other four `unittest` silently collects
  nothing and reports `OK` — 17 tests that no runner had ever executed. See
  `R-TST-11`.
- `CONTRIBUTING.md` lists "No test gate in CI" as a known gap. It never says how
  to run the tests.
- An unknown subset needs a live Postgres or model credentials, and nothing
  marks which.
- The one billable test is guarded, but not correctly: `test/test_async.py`
  refused to run by raising `SystemExit` at import, which aborted pytest's
  collection of the entire suite. See `R-TST-12`.

**Consequence.** Regressions reach `main` unless someone remembers the right
files. A contributor cannot run "everything that does not need a database".

**Fix.**
- pytest becomes the runner of record; it runs the existing `unittest` classes
  unchanged.
- Add markers `pg`, `billable` and `slow`, plus a CI job for
  `-m "not pg and not billable and not slow"`.
- Negative control: a deliberately failing test turns the job red.

**Verification.** The CI job green on a clean branch and red on the control.

---

## R-TST-02 — `sys.path` manipulation

**Severity** P2 · **Wave** W6 · **Status** Open

**Evidence.**
- Top-level tests: `test/test_async.py:30`, `test/test_ask_handler.py:16`,
  `test/test_fetch_verify.py:17`.
- `test/core/`: `test_agent_fingerprint.py:29`.
- `test/storage/`: `test_events_idempotent_put.py:138`.
- `test/consent/test_registry.py:17`.
- `test/async_subagents/`: `test_completion_loop.py:28`, `e2e_stack.py:49`,
  `test_unit.py:19`.
- `test/policy/`: `test_permission_parity.py:370`.

Scripts do the same: `scripts/verify_framework_contract.py:33-36`,
`scripts/verify_loop_affinity.py`.

**Consequence.** Import resolution depends on which file ran first. The package
is already installed editable, which is the mechanism that should be used.

**Fix.** Remove each manipulation. Helpers imported this way move to
`test/support/`.

---

## R-TST-03 — Test doubles copied between files

**Severity** P2 · **Wave** W6 · **Status** Open

**Evidence.** Class names defined in more than one test file: `_FakeStore` ×4,
`_FakeRegistry` ×3, `_FakeManager` ×3, `_FakeAgent` ×3, `_StubStore` ×2,
`_FakeConvo` ×2, `_FakeClient` ×2.

**Consequence.** When a protocol changes, some fakes are updated and some are
not. Tests then pass against doubles that no longer match production.

**Fix.** `test/support/fakes.py`, with each fake typed against its `ports`
protocol so a drift fails type checking.

---

## R-TST-04 — Tests reach into private names

**Severity** P2 · **Wave** W6 · **Status** Open

**Evidence.** 16 imports of underscore names:

| Test | Private names imported |
|---|---|
| `test/test_ask_handler.py:18` | `cli.commands.chat_repl._ask_handler` |
| `test/test_fetch_verify.py:19` | `agents.task_runner.tools._looks_like_html`, `_verify_download` |
| `test/task_runner/test_executor_paths.py:85` | `…tools._get_or_create_agent` |
| `test/llm/test_model_handle.py:232` | `llm.factory._registered_count` |
| `test/memory/test_store.py:12` | `memory.store._keyword_tokens` |
| `test/cli/test_terminal_front.py:152, 245` | `artifacts.tools._ok`, `chat_repl._startup_status_line` |
| `test/storage/test_events_foreign_keys.py:301` | `storage.events.schema._add_foreign_keys_v5` |
| `test/retrieval/test_find_duplicate_row_shape.py:69, 75, 82` | `retrieval.pg._by_name_or_position` |
| `test/daemon/test_usage_frames.py:132` | `converse._latest_usage_snapshot` |
| `test/skills/test_skill_discoverability.py:24` | `core.prompts._tool_discovery_section` |
| `test/policy/test_observer_parity.py:314, 330, 340` | `policy.adapter._latest_usage` |

**Consequence.** Tests pin implementation details, so behaviour-preserving
refactors (W3–W4) break tests that should have kept passing.

**Fix.** Test through the public API. Where a helper is a genuine unit (the
keyword tokenizer, the HTML sniffing), make it public in its module
(`R-IMP-07`).

---

## R-TST-05 — The test tree does not mirror the package

**Severity** P2 · **Wave** W6 · **Status** Open

**Evidence.**
- **Directories named differently from what they test:** `test/web/` tests
  `daemon/web/`; `test/task_runner/` tests `agents/task_runner/`.
- **Six test files at the top of `test/`:** `test_ask_handler.py`,
  `test_async.py`, `test_fetch_verify.py`, `test_filesystem_prompt_override.py`,
  `test_general_purpose_search_tools.py`, `test_ports_is_a_leaf.py`.
- **Helper modules sitting among tests:** `test/core/agent_fingerprint.py`,
  `test/async_subagents/e2e_stack.py`.
- **Import placement in tests:** module-level imports mid-file in three test
  files (`R-IMP-03`); 15 `E402`.
- **Deliberate exception:** `test/mcp_layer/`, because a `test/mcp/` package
  would shadow the MCP SDK. It stays, with a note.

**Fix.**
- `test/daemon/web/` and `test/agents/task_runner/`.
- Loose files move into their package's directory.
- Helpers move to `test/support/`.
- A `test/README.md` states the layout rule and the `mcp_layer` exception.

---

## R-TST-06 — Packages with no tests of their own

**Severity** P1 · **Wave** W6 · **Status** Open

**Evidence.** No test directory corresponds to these packages:
- `agents` (beyond `task_runner`), `artifacts`, `todoboard`, `tools`, `visuals`
- `audio_io`, `io`
- `events`, `platform`, `prefs`, `models`, `aio`

Where coverage exists it is incidental: storage parity suites for the todo
store, web tests for routers.

**Consequence.**
- **`platform/`** — the OS-invariance substrate for code the maintainer cannot
  run on every OS — has no direct tests.
- **The upcoming decompositions** have nothing to pin their behaviour: the
  `TodoExchange` split (`R-SOL-01`) and the voice merge (`R-PLC-07`).

**Fix.** Tests land with the waves that need them: characterization tests before
each W4 split, unit tests for each extracted unit. `platform/` gets
contract tests that run on every OS in CI.

---

## R-TST-07 — Function-body imports in tests

**Severity** P2 · **Wave** W6 · **Status** Open

**Evidence.** ruff `PLC0415` counts 469 in `test/` and `scripts/`.

**Assessment.** Many are legitimate. Importing the agent stack inside a test body
keeps collection fast, which this project relies on. So the rule applies with the
"measured start-up cost" reason as a directory-level allowance for `test/`. The
mid-file module-level imports (`R-IMP-03`) are not covered by that allowance.

---

## R-TST-08 — A test double nothing reads, so a unit test calls a live service

**Severity** P1 · **Wave** W6 · **Status** Open (found during W2)

**Claim.** `test/memory/test_embedder.py` injects its fake HTTP client by
assigning `emb._client`, but `Embedder` keeps one client per event loop in
`_clients` (`memory/embedder.py:43-49`) and never reads `_client`. The fake is
bypassed, and every test posts to the real configured embedding endpoint.

**Evidence.** Of 7 tests:
- **4 error:** the fake's `last_json` is still `None`, because it never received a
  request.
- **2 fail:** `test_dim_mismatch_raises` gets no `ValueError`, and
  `test_healthcheck_false_on_error` gets `True`. A real endpoint answered both.

The result is identical at `f4ec45c` and at the commit before W2 step 2, each run
in its own worktree.

**Consequence.** The file tests none of what it names. Its outcome depends on
whether an embedding server happens to be running, and a "unit" test quietly makes
network calls. A test that asserted the fake had been called would have failed
loudly the moment the injection point moved.

**Fix.**
- Give `Embedder` a seam the class actually uses: a client-factory constructor
  argument that tests pass.
- Have the tests assert the fake received each request.

**Verification.**
- The file passes with no embedding server running.
- Negative control: a fake that records nothing makes it fail.

---

## R-TST-09 — A test broken by a signature change, unnoticed

**Severity** P2 · **Wave** W6 · **Status** Open (found during W2)

**Claim.** `test/daemon/test_build_retrieval.py` calls `build_retrieval()` without
the keyword-only `workspace` argument the function now requires, so both of its
SQLite tests error with `TypeError` before asserting anything. The other two tests
skip without Postgres.

**Evidence.** The file reports 2 errors and 2 skipped tests. The result is
identical at `f4ec45c`, at the commit before W2 step 3, and on the working tree,
each run in its own worktree at the same moment.

**Consequence.** The retrieval builder has had no working test since its signature
changed, and nothing noticed because no test runs in CI (`R-TST-01`). This is the
decay that finding predicts, observed.

**Fix.** Pass a temporary workspace in both tests.

**Verification.** Both tests pass. A deliberately wrong `workspace` makes them fail,
proving they exercise the builder.

---

## R-TST-10 — A regression test that cannot import what it tests

**Severity** P1 · **Wave** W6 · **Status** Done (found during W2, fixed in W4)

**Landed.** The test builds the handler through `make_ask_handler` with a stub
renderer whose only method is `pause()`, and patches the module's `read_line`
rather than `builtins.input` — the handler stopped using `input()` when the
split view put stdin in raw mode, so the old patch target was a second reason
the file could never have passed. All five assertions kept. The negative control
this finding asked for was run: removing the blank-line skip from the handler
fails `test_blank_lines_skipped_then_session` and nothing else.

**Claim.** `test/test_ask_handler.py` imports `_ask_handler` from
`yuyutsava.cli.commands.chat_repl`. No module-level function has had that name
since `2c7357e` (2026-07-19). The handler is now the closure returned by
`make_ask_handler(renderer, console)` (`chat_repl.py:421`), so the import fails
and the file runs no test.

**Evidence.**
- `python -m unittest test.test_ask_handler` reports one error:
  `ImportError: cannot import name '_ask_handler'`.
- The result is identical at `f4ec45c` and on every W2 commit, each run in its own
  worktree.
- The test last changed in `2af69af` (2026-06-21), before the handler moved.

**Consequence.** As its docstring says, the file guards a real bug: under several
parallel asks, a stray blank stdin line was read as a rejection and turned an
approval into a denied DELETE. Nothing has checked that fix since July. Like
`R-TST-09`, this is decay that a CI test job (`R-TST-01`) would have caught the
same day.

**Fix.** Build the handler through `make_ask_handler` with a stub renderer, and
keep the assertions.

**Verification.** The file passes. Removing the blank-line skip from the handler
makes it fail.

---

## R-TST-11 — Five files no runner ever ran, four of them reporting success

**Severity** P0 · **Wave** W4 · **Status** Done (found during W4)

**Claim.** 17 test functions in five files were written as bare `def test_*`
with no `TestCase`. `unittest` discovers nothing in them and exits **OK**, so
they counted as passing everywhere — including this review's own per-file
sweeps.

**Evidence.** An AST scan for modules with `test_*` functions and no `TestCase`
subclass: `framework_contract/test_deepagents_contract.py` (5),
`test_filesystem_prompt_override.py` (2), `test_general_purpose_search_tools.py`
(4), `web/test_converse_ws.py` (4), `web/test_voice_ws.py` (2). Only the last
fails loudly, because it imports `pytest` at module level; the other four print
`Ran 0 tests ... OK`.

**Consequence.** A green sweep line meant nothing for these files. The W3 commit
messages in this branch cite `test_converse_ws.py` and
`test_general_purpose_search_tools.py` as green; they had not executed. Worse
than a red test is a runner that reports success over zero tests.

**Fix / Landed.** pytest declared (`[project.optional-dependencies] dev`) and
installed; it collects both styles. On first execution: **14 pass, 3 fail**, and
all three failures are stale tests, not product defects —
`test_converse_ws.py::test_inline_ask_roundtrip` asserts
`ask["payload"]["body"]` while both emitters send `{"type":"ask","ask":{...}}`
(`routers/converse.py:587`, `services/stream_service.py:101`), and
`test_voice_ws.py`'s `_FakeVoice` lacks `prewarm`, which production has called
since before `f4ec45c`. Both confirmed pre-existing: the converse one fails
identically in a `f4ec45c` worktree, and `git show f4ec45c` shows `prewarm()`
already called against a fake that never had it.

**These two need rewriting, not patching — attempted and reverted in W4.** Each
single-layer repair moves the file from a fast failure to an indefinite hang,
because both encode a router flow that no longer exists:

- Giving `_FakeVoice` the missing `prewarm` lets `_ensure_voice()` succeed, and
  the test then blocks forever waiting for `speech_started`: with barge-in
  disabled the `audio` branch ignores mic input for the whole duration of an
  active turn (`converse.py:1033-1042`), so `feed_audio` is never reached. The
  test predates that gate.
- In `test_converse_ws.py` the ask assertion has three independent stale layers,
  not one: the frame key (`payload` → `ask`), the field the stub feeds the
  interrupt (`body` → `question`, since `body_for_interrupt` reads
  `iv["question"]` for a `user_question`), and a third that hangs the socket
  once the first two are corrected.

Both were reverted so the suite keeps failing in under a second rather than
hanging, which is what a whole-suite run depends on. W6 rewrites them against
the current frame contract; `R-RDB-01`'s characterization net is written fresh
rather than inherited from them.

**Verification.** `pytest --collect-only test` reports 1,721 tests, 0 errors.
The three stale tests are fixed in W6 with the other test debt.

---

## R-TST-12 — The billable guard stopped the whole suite from collecting

**Severity** P1 · **Wave** W4 · **Status** Done (found during W4)

**Claim.** `test/test_async.py` refused to run by raising `SystemExit` at import
time. pytest imports every test module during collection, so that one statement
aborted collection of all 1,587 otherwise-collectable tests with
`INTERNALERROR`.

**Consequence.** Adopting pytest looked impossible: the runner appeared broken
on the whole tree when in fact one module was exiting during import.

**Fix / Landed.** Skip at module level when pytest is the importer
(`sys.modules.get("pytest")` — *importing* pytest, not merely having it
installed), and keep `SystemExit` for a direct run. The distinction matters:
calling `pytest.skip()` outside a pytest session raises `Skipped`, which exits
**0** — a bulk `for f in test/*.py` sweep would have read that as success, which
is how this script got billed on 2026-08-08.

**Verification.** Collection is clean; `python test/test_async.py` still refuses
with exit 1 and no traceback.

---

## R-TST-13 — A cached default store makes three files fail, but only together

**Severity** P1 · **Wave** W6 · **Status** Open (found during W4)

**Claim.** `test/daemon/test_build_storage.py` calls `build_storage`, which
calls `set_default_session_store(PgSessionStore(storage, pool=pg_pool))`
(`daemon/bootstrap.py:442`), writing the module-level `_DEFAULT_STORE`
(`storage/sessions/sqlite_impl.py:374`). Its `tearDown` restores
`YUYUTSAVA_STATE_DIR` and `YUYUTSAVA_STORAGE_BACKEND` faithfully — but an
environment variable cannot un-cache an object.

**Evidence.** In a whole-suite run, `storage/test_app_context.py`
`::test_full_purge_never_consults_a_global`, `storage/test_feedback_purge.py`
and `web/test_todo_board_dependency.py` (8 subtests) fail with
`RuntimeError: PgPool.open() must be called first`. Each passes alone, under
both runners. Bisecting by directory and then by file names
`test_build_storage.py` as the only predecessor that causes it.

**Consequence.** This is `R-GLB`'s `set_default_*` family with a bill attached:
six setters fire inside `build_storage`. It was undetectable by construction
while every check ran one file per process — which is what every sweep in this
review did until W4.

**Fix.** W6: the setters get a documented reset for tests, or the fixtures stop
relying on process-wide defaults (the `AppContext` seam already exists and is
what `test_app_context` is asserting about). Not a W4 refactor.

**Verification.** `pytest test/daemon/test_build_storage.py
test/storage/test_app_context.py` passes.

---

## R-TST-14 — A standing failure that was never in the ledger

**Severity** P1 · **Wave** W6 · **Status** Open (found during W4)

**Claim.** `retrieval/test_find_duplicate_row_shape.py`
`::TheRealWritePath::test_identical_text_is_deduplicated` fails in isolation
under both runners: two identical writes produce two rows, and the message says
it plainly — "find_duplicate returned None, so the dedup probe is not actually
running".

**Evidence.** The known-red list in this review named four files
(`R-TST-08`…`10`, plus `test_voice_ws`). This is a fifth. The test file is
unchanged on `code-improvement` apart from the tree-wide `ruff format`
(`a309b8a`), so it pre-dates the branch.

**Consequence.** Unlike the others this is a claim about *product* behaviour —
memory dedup may not be deduplicating — not a broken test. It needs Postgres to
reproduce.

**Fix.** W6, with `R-TST-08`/`09`: classify against a clean baseline, then fix
the dedup probe or the test's premise.

---

## R-SCR-01 — Maintainer scripts are undocumented

**Severity** P2 · **Wave** W6 · **Status** Open

**Evidence.**
- `scripts/` holds eight tools and an SQL init file.
- `CONTRIBUTING.md` documents three of them.
- `benchmark_policy_adapter.py`, `measure_framework_surface.py`,
  `langfuse_register_model.py` and `migrate_sessions_sqlite_to_pg.py` are
  described nowhere.
- `scripts/pg-init/01-create-langfuse-db.sql` is compose infrastructure, not a
  tool.
- Two scripts manipulate `sys.path` (`R-TST-02`).
- This review adds `verify_imports.py` and `measure_code_health.py`.

**Fix.** `scripts/README.md` listing each script, what it proves, when to run it,
and its exit-code contract (STD-SCR-1).

---

## What is already good

These are the parts every change should protect, and imitate:

- **`test/core/test_agent_fingerprint.py`:** proves agent assembly byte-identical
  across nine configurations.
- **`scripts/verify_framework_contract.py` and `test/framework_contract/`:**
  catch the framework couplings that fail silently, each tripwire observed
  failing on a deliberate break.
- **`test/storage/test_twin_conformance.py` and the parity suites:** turned the
  storage collapse into a mechanical change.
- **`test/test_ports_is_a_leaf.py`:** keeps the protocol layer acyclic.
- **The billable-call guard in `test/test_async.py`.**
