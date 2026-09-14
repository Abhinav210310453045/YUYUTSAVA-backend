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

**Severity** P1 · **Wave** W6 · **Status** Open

**Claim.** The suite cannot be run safely by a machine.

**Evidence.**
- 115 files use `unittest`; 2 import `pytest`. There is no `conftest.py` and no
  `[tool.pytest.ini_options]`.
- `CONTRIBUTING.md` lists "No test gate in CI" as a known gap. It never says how
  to run the tests.
- An unknown subset needs a live Postgres or model credentials, and nothing
  marks which.
- The one billable test is guarded correctly: `test/test_async.py` refuses to
  run without `YUYUTSAVA_ALLOW_BILLABLE=1`.

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
