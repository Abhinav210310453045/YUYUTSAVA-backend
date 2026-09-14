# 03 — Imports and packages

Where imports sit, what they break, and what each `__init__.py` does.
Rules: [01 §1–2](01-standards.md). Numbers: [02](02-metrics.md).

**Headline.** Six modules on `main` crash when they are the first thing
imported. Import placement carries hidden behaviour: 415 imports sit inside
function bodies, most without a reason. Their real cause is one eager
`__init__.py`.

---

## R-IMP-01 — Six modules crash when imported first

**Severity** P0 · **Wave** W2 · **Status** Open

**Claim.** `yuyutsava.skills.registry`, `yuyutsava.skills.tools`,
`yuyutsava.tools.search`, `yuyutsava.todoboard.block_audio`, `…block_journey` and
`…block_jsx` each raise `ImportError: … partially initialized module … (most likely
due to a circular import)` when imported in a fresh interpreter.

**Evidence.** `scripts/verify_imports.py` at `f4ec45c`: 387 modules, 6 failures.
The three cycles, traced:

| Entered from | Chain | Root cause |
|---|---|---|
| `skills/registry.py:23` | → `core/__init__.py:12` → `core/engine.py:54` → `agents/__init__.py:10` → `agents/base_sub_agent.py:48` → back to `skills.registry` | eager `core/__init__.py` (`R-INIT-01`) and `agents/__init__.py` (`R-INIT-02`) |
| `tools/search.py:28` | → `core/__init__.py:12` → `core/engine.py:58` → back to `tools.search` | eager `core/__init__.py` |
| `todoboard/block_audio.py:23` | → `todoboard/artifacts.py:227` → back to `block_audio` | mid-file import at line 227 (`R-IMP-03`) |

**Consequence.** Production works only because some other module happens to load
first. Anything that imports one of these first crashes before running a line:
a new test, a script, a subprocess, or a refactor that reorders imports. Worse,
each "move this import to the top" clean-up turns another latent cycle into a
crash, so hygiene work is dangerous until the cause is removed.

**Fix.** `R-INIT-01` (docstring-only `core/__init__.py`), `R-INIT-02` (slim
`agents/__init__.py`), and `R-IMP-03` (explicit block registration).

**Verification.** `scripts/verify_imports.py` reports 0 failures, and stays at
zero after every later wave.

---

## R-IMP-02 — Imports inside function bodies, mostly without a reason

**Severity** P1 · **Wave** W2 · **Status** Open

**Claim.** 415 import statements sit inside function bodies across 115 files. The
internal ones are the tell: 24.6% of all first-party imports are deferred.

**Evidence.** `scripts/measure_code_health.py`:

| Kind | Inside a function | …of which inside `try` |
|---|---|---|
| First-party | 288 | 21 |
| Third-party | 101 | 43 |
| Standard library | 26 | 6 |

Top files: `core/engine.py` 45 · `cli/commands/chat_repl.py` 29 ·
`cli/agent_stack.py` 23 · `cli/render/dashboard.py` 19 · `daemon/bootstrap.py` 18 ·
`storage/factory.py` 14.

Classified by reading each one (reason (a) breaks a real cycle, (b) optional or
heavy dependency, (c) no reason):

- **`core/engine.py` (45):** 11 (a), every one of them through `core/__init__.py`;
  1 (b); **33 (c)**. The comment at `core/engine.py:762-763` justifies three of
  them by "keep engine.py importable where langgraph_api is absent", but those
  three modules import only `yuyutsava.policy.*`.
- **CLI hot spots** (`chat_repl.py`, `agent_stack.py`, `dashboard.py`,
  `agents/tinker/agent.py`, `cli.py`; 83 statements): **0 (a)**, 28 (b), **55 (c)**.
- **Re-imports of names already imported at the top:** `cli/agent_stack.py:350-351`
  (`core.engine` is imported at line 31), `agents/tinker/agent.py:123` (`asyncio`,
  already line 18) and `:141-142`, `core/engine.py:643` and `:1076` (already lines
  46 and 57), `dashboard.py:324` (`FormattedTextControl`, already line 71).
- **Standard library in function bodies:** `platform/elevation.py:94,139,146,159,200`,
  `core/tracing.py:154-155`, `audio_io/announcer.py:98`, `cli/render/context_panel.py:257`.
  `io/wake.py:134-137` imports `time` and `numpy` inside `process()`, which runs
  for every 80 ms audio chunk.
- **Swallowed import failures:** `context/meter.py:500-517` wraps first-party
  imports in `except Exception: pass`, so a renamed constant silently drops a
  prompt segment. `cli/agent_stack.py:265-269` reports a missing module as a
  "price refresh failure" at DEBUG.

**Consequence.** A module's dependencies cannot be read from its header. The
position of a statement inside a function is load-bearing, and no tool checks
it. Import machinery runs on hot paths. `R-IMP-01` shows what happens when the
placement shifts.

**Fix.** Apply STD-IMP-2. After `R-INIT-01`, move every (a) and (c) import to the
top of its module, one package per commit. Keep (b) imports with
`# noqa: PLC0415  <reason>`.

**Verification.** Deferred first-party import ratio below 8%. ruff `PLC0415`
allowlist holds only reasoned entries. `verify_imports.py` green after every
commit. Agent fingerprint byte-identical.

---

## R-IMP-03 — Module-level imports after code and in the middle of files

**Severity** P1 · **Wave** W2 · **Status** Open

**Claim.** Five module-level imports sit below the first `def`/`class` of their
file, and 31 sit below executable statements.

**Evidence.**

- `todoboard/artifacts.py:227-229` imports `block_audio`, `block_jsx` and
  `block_journey` after 200 lines of definitions, under `# noqa: E402`, so that
  their blocks register when `artifacts` is imported. This is the cycle behind
  `R-IMP-01`: each block module imports `artifacts` back.
- `storage/sessions/sqlite_impl.py:346-347` imports `storage.backend` and
  `storage.sessions.store` after the class definitions, to build a default store.
- `cli/cli.py:25` and `daemon/main.py:35` run
  `os.environ.setdefault("GRPC_VERBOSITY", "ERROR")` and a `dotenv` `try` block
  before the rest of their imports (`cli.py:27-38`, `main.py:37-58`). Nothing
  shows that gRPC is loaded by those imports, so the ordering may be unnecessary.
- Tests: `test/core/agent_fingerprint.py:633`,
  `test/test_general_purpose_search_tools.py:74-75`,
  `test/context/test_lossless_readback.py:87`.

**Consequence.** A reader scanning the header misses the dependency; the import
order becomes behaviour; the block registry depends on a side effect of importing
a different module.

**Fix.**
- Built-in blocks register through an explicit `register_builtin_blocks()` in a
  module that imports the block modules at its top.
- The default-store accessor moves beside its factory.
- For the gRPC setting, first check whether `grpc` is in `sys.modules` after the
  entry points' imports. If it is not, the setting moves below the imports. If
  it is, one bootstrap module imported first by both entry points owns it.

**Verification.** `measure_code_health.py`: `mid_file_imports` 0 and
`imports_after_code` 0 (or one documented bootstrap). ruff `E402` gate green.

---

## R-IMP-04 — Names used in annotations are never imported

**Severity** P1 · **Wave** W1 · **Status** Done (`4599d33`, `08cce31`)

**Claim.** 35 annotations reference names that are not defined in their module
(ruff `F821`).

**Evidence.**

| Where | Name | Count |
|---|---|---|
| `daemon/web/routers/todos.py:72-514` | `TodoExchange` | 26 |
| `core/engine.py:616,617,705,707,852` | `Sequence` | 5 |
| `agents/task_runner/zones.py:49-50` | `Path` | 2 |
| `core/engine.py:1182` | `OrchestratorDeps` | 1 |
| `daemon/bootstrap.py:493` / `:942` | `CapEnforcer` / `MirroredTask` | 2 |

**Consequence.** `todos.py` is a FastAPI router, and FastAPI evaluates handler
annotations. The handlers work only because each parameter's `Depends(...)`
default hides the unresolved type. `typing.get_type_hints` on any of them raises
`NameError`, and so does any tool that introspects signatures. For example,
`typing.get_type_hints(zones.classify_zone)` raises today.

**Fix.** Import each name under `if TYPE_CHECKING:`.

**Verification.** ruff `F821` gate at 0 in CI, and negative-controlled.

---

## R-IMP-05 — `__all__` exports classes that no longer exist

**Severity** P2 · **Wave** W1 · **Status** Done (`4599d33`)

**Claim.** `todoboard/store.py:225-226` lists `SqliteTodoStore` and `PgTodoStore`
in `__all__`; both classes were deleted (ruff `F822`).

**Consequence.** `from yuyutsava.todoboard.store import *` raises
`AttributeError`, and the export list documents an API that does not exist.

**Fix / Verification.** Remove the entries; ruff `F822` gate.

---

## R-IMP-06 — Unused and unsorted imports

**Severity** P2 · **Wave** W1–W2 · **Status** Open

**Evidence.** ruff 0.16.5 over `yuyutsava test scripts`: `F401` unused imports 134,
`I001` unsorted import blocks 123. One `noqa` is wrong:
`conversation/service.py:33` claims `Callable` is only used "in type strings", but
it is used at runtime at lines 50-51.

**Fix / Verification.** `ruff check --select F401,I001 --fix` as a mechanical
commit; both rules gated afterwards.

---

## R-IMP-07 — Private names imported across package boundaries

**Severity** P1 · **Wave** W2 · **Status** In progress: 14 → 8 (`2eff394` removed the six `core.config._env` imports)

**Claim.** 14 imports take an underscore-prefixed name from another sub-package.

**Evidence.**

| Importer | Private name |
|---|---|
| `context/config.py:15`, `memory/config.py:19`, `cli/agent_stack.py:30`, `agents/tinker/agent.py:30`, `daemon/bootstrap.py:43`, `daemon/resources.py:35` | `core.config._env` |
| `agents/tinker/agent.py:28` | `cli.agent_stack._build_retrieval_stores` — the agents layer depending on the CLI |
| `agents/tinker/prompts.py:17` | `core.prompts._rules_section`, `_tool_discovery_section` |
| `conversation/service.py:38` | `sessions.runner._CoalescedTicker`, `_count_memory_files`, `_resolve_session` |
| `core/engine.py:1205` | `agents.orchestrator.agent._make_ask_user_tool` |
| `daemon/web/routers/sessions.py:24` | `core.streaming._artifact_event_from_result` |

Tests add 16 more (see `R-TST-04`).

**Consequence.** The underscore promises "safe to change", but another package
depends on the name. A rename breaks a distant module, and no contract
documents the coupling.

**Fix.** Each name becomes public API in the package that owns the concept:
`yuyutsava/config/env.py` for environment parsing (which also absorbs `R-DRY-01`),
`storage/factory.build_retrieval_stores`, `make_ask_user_tool`,
`artifact_event_from_result`, public prompt sections, and the conversation helpers.

**Verification.** `private_cross_package_imports` 0; the private-import check in
`test/architecture/test_import_layers.py`.

---

## R-IMP-08 — Private third-party APIs

**Severity** P2 · **Wave** W2 · **Status** Open

**Evidence.** `cli/commands/chat_repl.py:99` imports
`langgraph_api.cli._check_newer_version`; `chat_repl.py:203` calls
`core.tracing._langfuse_reachable()`.

**Consequence.** A patch release of `langgraph-api` can remove the function; the
REPL banner then fails at start-up.

**Fix.** Use a public API or drop the version notice; expose a public tracing probe.

---

## R-INIT-01 — `core/__init__.py` loads the whole agent stack

**Severity** P0 · **Wave** W2 · **Status** Done (`2ce0c1c`)

**Claim.** `core/__init__.py:5-21` eagerly imports `core.engine`, `core.streaming`
and `core.docker_sandbox_backend`. Python runs a package's `__init__.py` before
any of its modules, so every import of anything under `yuyutsava.core` pays for
the full agent stack.

**Evidence.**

| Import | Time (warm) | Modules loaded | LangChain loaded |
|---|---|---|---|
| `yuyutsava.core.config` | 532 ms (860 ms cold) | 2,398 | yes |
| `yuyutsava.llm.providers` (imports `core.config`) | 651 ms (1,314 ms cold) | 2,987 | yes |
| `yuyutsava.memory.store` | 559 ms | 2,519 | yes |
| `yuyutsava.context.config` | 555 ms | 2,524 | yes |
| `yuyutsava.storage.paths` (for contrast) | 7 ms | 94 | no |

- **Cycles:** all 11 cycle-breaking imports in `core/engine.py` exist because of
  this file, and it causes two of the three failures in `R-IMP-01`.
- **Consumers:** only one imports a name from the package root,
  `test/test_async.py:35`. Every other root import takes a submodule
  (`from yuyutsava.core import engine`). 8 of the 11 exported names have no
  root consumer.
- **A broken promise:** `core/text_utils.py:3-5` says it is "safe to import from
  any layer". Because of this `__init__.py`, it is not.

**Consequence.** Settings, paths and small helpers cannot be imported cheaply.
Cycles run through `core` from every direction, and each workaround adds a
deferred import.

**Fix.** Reduce `core/__init__.py` to a docstring. Change `test/test_async.py:35`
to submodule imports.

**Verification.**
- `import yuyutsava.core.config` in under 100 ms without LangChain.
- `verify_imports.py` shows `skills.registry` and `tools.search` fixed.
- Agent fingerprint byte-identical.

---

## R-INIT-02 — Other eager package initialisers

**Severity** P1 · **Wave** W2 · **Status** Done (`b0b73e3`)

| `__init__.py` | Eagerly loads | Root consumers |
|---|---|---|
| `agents/__init__.py:10-11` | `base_sub_agent` → the MCP SDK (`mcp.loader`), `task_runner` | none |
| `context/__init__.py:24-27` | `compaction` → LangChain/LangGraph; `context.config` → `core` | none |
| `memory/__init__.py:23-25` | `memory.config` → `core.config` → the agent stack | none |
| `mcp/__init__.py:13-15` | `loader` → the third-party `mcp` SDK | none |

**Consequence.** `aio/__init__.py:3-5` calls `memory` a leaf, and importing
`yuyutsava.mcp.config` alone loads the MCP SDK. Neither module is as light as
its name suggests.

**Fix / Verification.** Docstring-only initialisers (STD-PKG-2, STD-PKG-4);
`verify_imports.py --slowest`.

---

## R-INIT-03 — Package exports nobody uses

**Severity** P2 · **Wave** W2 · **Status** Done (`c4a7475`, `c2ebd89`, `12bf744`; metric corrected in `50b65f9`)

**Claim.** 24 packages export names through `__all__` that no module, test or
script imports from the package root.

**Evidence.** Unused exports from root:

| Package | Unused of exported |
|---|---|
| `models` | 17 of 17 |
| `artifacts` | 8 of 8 |
| `llm.providers` | 8 of 9 |
| `core` | 8 of 11 |
| `retrieval` | 7 of 7 |
| `policy` | 5 of 5 |
| `consent` | 5 of 12 |
| `platform` | 5 of 9 |
| `context`, `memory`, `visuals`, `audio_io` | 4 each |
| `agents`, `mcp`, `storage.routing`, `discovery` | 3 each |
| 8 more | 1–2 each |

`platform/__init__.py:17-18` instructs readers to import from the package root,
yet its consumers import `platform.process` and `platform.elevation` directly.

**Consequence.** Two import styles for the same names, and package docs that
describe an API nobody uses.

**Fix / Verification.** STD-PKG-2; `inits_with_unused_exports` 0.

---

## R-INIT-04 — Logic in an `__init__.py`

**Severity** P2 · **Wave** W2 · **Status** Done (`6e83f16`; the `__init__.py` re-exports nothing, because no consumer imports `provider_for` from the package root)

**Evidence.** `llm/providers/__init__.py` constructs the provider instances, holds
the `_PROVIDERS` tuple and defines `provider_for`.

**Fix.** Move them to `llm/providers/registry.py`; the `__init__.py` re-exports
`provider_for`.

---

## R-INIT-05 — Packages without a docstring

**Severity** P2 · **Wave** W2 · **Status** Done (`b7478e2`)

**Evidence.**
- 0-byte `__init__.py`: `agents/face_watcher/`, `daemon/web/routers/`,
  `daemon/web/schemas/`, `daemon/web/services/`.
- A `#` comment instead of a docstring: `tools/__init__.py`.

**Fix / Verification.** STD-PKG-1; `inits_without_docstring` 0.

---

## R-INIT-06 — Package docstrings that contradict the code

**Severity** P2 · **Wave** W2 · **Status** Done (`b0b73e3`, `c4a7475`, `8863a0f`)

| `__init__.py` | Says | Reality |
|---|---|---|
| `cli/render/__init__.py:5-8` | `ChatRenderer` is "in chat_repl.py"; the package is "only imported when … rich_capable" | it is `cli/render/plain.py:48`; `chat_repl.py:40` imports the package unconditionally |
| `storage/__init__.py` | a layout list with "(Step 2)" markers; "nothing under yuyutsava.core may import from this package" | the list is stale; `core` imports `storage` 11 times, 10 at module level |
| `context/__init__.py:3-6` | two of the three layers "are still middleware" | `BudgetPolicy` is a `Policy` (`daemon/budget_policy.py:19`) |
| `memory/__init__.py:14` | `yuyutsava.memory.injector.MemoryInjector` | no such module; the class is `context/injector.py:31` |
| `retrieval/__init__.py:11-12` | the embedder "is re-exported here" | it is not |
| `policy/__init__.py:12-13` | "Nothing in … a migrated policy imports a framework" | `core/filesystem_prompt_policy.py:34-35` imports `deepagents` at module level |
| `ports/__init__.py:13-14` | "68 in `core/engine.py` alone" | 45 today |
| `agents/orchestrator/__init__.py:1` | "react-style router that dispatches to subagents" | `orchestrator/agent.py:2-4`: "definitions only" |

**Fix / Verification.** Rewrite each to describe current behaviour (STD-DOC-1, STD-DOC-3).
