# 01 — Standards

The rules code in this repository follows. They are **normative**: a change that
breaks one is reviewed as a defect, whatever the surrounding code does. Each rule
states why it exists and how it is enforced; "(W*n*)" marks the remediation wave
in [11](11-remediation-plan.md) that introduces the enforcement.

These rules replace "match the surrounding code" as the default. The surrounding
code is what [03–09](README.md#how-to-read) found wanting.

---

## 1. Imports

**STD-IMP-1 — Imports go at the top of the module.** After the module docstring
and `from __future__ import annotations`, in three sorted groups: standard
library, third-party, first-party. Nothing else comes before them.
*Why:* a reader learns a module's dependencies in one place; an import at
line 227 is a dependency nobody sees until it fails.
*Enforced:* ruff `E402`, `I001` (W1, W2).

**STD-IMP-2 — A function-level import is an exception with a stated reason.**
Allowed reasons form a closed list:

| Reason | Example |
|---|---|
| Optional extra that may not be installed | `faster_whisper`, `exa_py`, `opentelemetry`, `langfuse` |
| Platform-specific module | `ctypes.windll` inside `yuyutsava/platform/` |
| Measured start-up cost on a named CLI fast path | the daemon stack behind `yuyutsava daemon` |
| Entry module of a subprocess | `events/sources/_voice_proc.py` |

The import line carries `# noqa: PLC0415  <reason>`. **Never allowed:** a
standard-library import; a first-party import used to dodge a circular import
(fix the layering, rule STD-LAY-1); re-importing something already imported at
the top.
*Enforced:* ruff `PLC0415` with a shrinking allowlist (W1, W2).

**STD-IMP-3 — No circular imports, including order-dependent ones.** Every
module must import first, alone, in a fresh interpreter.
*Why:* a cycle that only breaks when entered from one side is invisible in normal
use and becomes a crash the moment an import moves.
*Enforced:* `scripts/verify_imports.py` (W0).

**STD-IMP-4 — Every annotation name is imported.** Annotation-only imports go
under `if TYPE_CHECKING:`; quoted annotations are not an exemption.
*Why:* `typing.get_type_hints`, FastAPI and type checkers evaluate annotations.
*Enforced:* ruff `F821` (W1).

**STD-IMP-5 — Never import an underscore name from another package.** If two
packages need it, it is public API: rename it and document it. Tests follow the
same rule (STD-TST-4).
*Enforced:* private-import check in `test/architecture/test_import_layers.py` (W1).

**STD-IMP-6 — Importing a module does no work.** Module level holds definitions
and I/O-free registrations only. Environment mutation belongs in an entry point.

---

## 2. Packages and `__init__.py`

**STD-PKG-1 — Every package has an `__init__.py` with a docstring** stating what
the package owns (and, where it helps, what it deliberately does not).
Empty files and `#` comments are not docstrings.

**STD-PKG-2 — Re-export only what consumers import from the package root.**
`__all__` lists exactly the re-exports. If no consumer imports from the root, the
`__init__.py` is docstring-only.

**STD-PKG-3 — No logic in `__init__.py`**: no functions, classes, registries,
instances or I/O. A registry belongs in a named module (`registry.py`).

**STD-PKG-4 — An `__init__.py` never eagerly imports a heavy submodule.**
Importing `yuyutsava.<pkg>.<leaf>` costs only that leaf's own dependencies.
Budget: a settings, paths or data-model module imports in under 100 ms without
loading LangChain.
*Why:* Python runs every parent `__init__.py` before the leaf; one eager import
in `core/__init__.py` makes `import yuyutsava.core.config` load the entire agent
stack and closes eleven import cycles (`R-INIT-01`).
*Enforced:* `scripts/verify_imports.py --slowest` and review (W2).

**STD-PKG-5 — Names never shadow the standard library or an installed
top-level package** (`io`, `platform`, `code`, `math`, `mcp`). Existing
exceptions and their status are listed in [04](04-placement-and-layering.md).

---

## 3. Layering

A module imports only from its own layer or a lower one. When a lower layer needs
behaviour from a higher one, it declares a Protocol in `ports/` and a composition
root injects the implementation.

| Layer | Packages |
|---|---|
| **L0** — leaves | `ports`, `models`, `aio`, `platform`, `config` (environment helpers and dependency-free settings), `text` (text helpers) |
| **L1** — foundations | `storage.paths`, `storage.ids`, `storage.dialect`, `llm`, `run_context` (the active run's thread and agent path) |
| **L2** — domains and stores | `storage`, `retrieval`, `memory`, `skills`, `prefs`, `visuals`, `artifacts`, `events`, `voice` (backends plus the pipeline built on them), `websearch` (the `ws_*` tools), `discovery` (imports nothing else from the package; `skills` builds on it) |
| **L3** — agent capabilities | `policy`, `context`, `mcp`, `consent`, `docker_sandbox` |
| **L4** — agent assembly | `core`, `agents`, `conversation`, `todoboard`, `async_subagents` |
| **L5** — composition roots | `cli`, `daemon`, `channels` |

**STD-LAY-1 — Imports point down the table.** *Enforced:* `test/architecture/test_import_layers.py`,
whose allowlist of today's violations may only shrink (W1, W3).

**STD-LAY-2 — `ports/` imports nothing first-party.** *Enforced:* `test/test_ports_is_a_leaf.py`.

**STD-LAY-3 — Only composition roots know concrete implementations of more than
one domain.** Everything else receives its collaborators.

---

## 4. Placement

**STD-PLC-1 — Code lives with the concept it serves, not with its first caller.**

| When you write… | …it goes in |
|---|---|
| A family of LangChain tools | `<domain>/tools.py`, exposing `make_<domain>_tools()` |
| A `Policy` | `policy/`; built-in policies in `policy/builtin/` |
| A persisted store | its domain package; constructed only by the store factory (`core/stores.py`) |
| An HTTP endpoint | `daemon/web/routers/` — parse, call a service, map the result |
| Request and response models | `daemon/web/schemas/` |
| Endpoint logic | `daemon/web/services/` |
| Environment or settings parsing | `config/` helpers — never an ad-hoc `os.environ.get(...).lower() in (...)` |
| OS-specific primitives | `platform/` |
| Terminal output | `cli/render/` |
| Provider-specific model behaviour | `llm/providers/` or `llm/quirks/` |
| A Protocol shared across layers | `ports/` |
| A plain data record | `models/` |

**STD-PLC-2 — `core/` is agent assembly only.** CLI rendering, daemon wiring,
pricing, persistence and provider knowledge live elsewhere.

**STD-PLC-3 — A module used by one package lives in that package.**

**STD-PLC-4 — No dead code.** Unused modules, functions, re-exports,
compatibility shims without a consumer and abandoned features are deleted; git
keeps the history.

---

## 5. Naming

**STD-NAM-1 — One concept, one name; one name, one concept.** Four unrelated
things called "Policy" is a violation.

**STD-NAM-2 — A module's name describes what it contains now.** A module named
after a class it no longer holds is renamed.

**STD-NAM-3 — Command-line flags are kebab-case; environment variables are
`YUYUTSAVA_*` upper snake case.**

**STD-NAM-4 — A leading underscore means "not imported from elsewhere".**

---

## 6. Size and shape (KISS)

| Measure | Target | Hard limit | Enforced by |
|---|---|---|---|
| Function body | ≤ 60 lines | 100 lines | `measure_code_health.py`, review |
| Cyclomatic complexity | ≤ 15 | 25 | ruff `C901` (W1, W4) |
| Parameters | ≤ 7 | — beyond 7, use a parameter object | ruff `PLR0913` (W1, W4) |
| Nesting depth | ≤ 4 | — | review |
| Class | ≤ 400 lines, one reason to change | — | review |
| Module | ≤ 600 lines | — | review |

**STD-KIS-2 — No feature-sized closures.** A factory returning tools defines
them at module level, or as methods of a small class holding the bound context.

**STD-KIS-3 — No speculative generality.** No parameter, flag, protocol method
or capability without a caller today.

**STD-KIS-4 — A boolean parameter that selects behaviour becomes two functions
or an enum.**

**STD-KIS-5 — No magic numbers.** Timeouts, limits and sizes are named constants
beside their use, or settings.

---

## 7. Docstrings and comments

**STD-DOC-1 — Describe current behaviour and contract, not history.** No
"Phase 4", "Step 2.5b", "was:", "used to", "no longer", "replaces". History
belongs in commit messages and `CHANGELOG.md`.

**STD-DOC-2 — Comments explain why.** A comment restating the code is noise; a
comment recording a hard-won constraint is the most valuable line in the file.

**STD-DOC-3 — A docstring that contradicts the code is a bug**, fixed in the same
commit as the behaviour it describes.

**STD-DOC-4 — Cite decisions by ADR path.** Never cite a plan document that is
not in the repository.

**STD-DOC-5 — Avoid counts in prose** ("the four tools"). If a count matters, a
test asserts it.

*Enforced:* review, plus the history-phrase count in `measure_code_health.py`
reaching zero (W4).

---

## 8. Errors and logging

**STD-ERR-1 — Never `except Exception: pass`.**

**STD-ERR-2 — Catch the narrowest exception the code can actually handle.**

**STD-ERR-3 — A deliberately broad catch marks a boundary** — a turn loop, a
background task, a plugin, a teardown. It logs with `exc_info=True`, and carries
`# noqa: BLE001  <reason>`.

**STD-ERR-4 — Re-raise with `raise … from exc`.**

**STD-ERR-5 — Library code logs through `logging.getLogger(__name__)`.** `print`
appears only in `cli/` and in `scripts/`.

**STD-ERR-6 — One exception type per failure concept.**

*Enforced:* ruff `S110`, `BLE001`, `B904`, `T201` (W1, W5).

---

## 9. State and dependencies

**STD-DEP-1 — No mutable module-level singletons** and no
`set_default_*`/`get_default_*` accessors. Dependencies are passed in — through a
constructor, FastAPI `Depends`, or the `AppContext`.

**STD-DEP-2 — Dependency parameters are typed with a `ports` Protocol**, never
`Any` or `object` with the real type in a comment.

**STD-DEP-3 — More than seven dependencies become a typed dataclass.**

---

## 10. Reuse (DRY)

**STD-DRY-1 — One implementation per concept.** Canonical helpers:

| Concept | Canonical home (after W2–W4) |
|---|---|
| Reading environment variables | `yuyutsava/config/env.py` — `env`, `env_flag`, `env_int`, `env_float` |
| Flattening message content to text | `yuyutsava/text.py` — `flatten_content` |
| Thread id of the current run | one helper beside the policy adapter |
| Truncating display text | `cli/render/tool_format.truncate` |
| Terminal colours | `cli/render/palette.py` |
| WAV reading and writing | the voice package |

**STD-DRY-2 — Search before writing a helper.** When a second copy is found, the
change that found it consolidates it.

**STD-DRY-3 — Duplicated knowledge is worse than duplicated code.** A list of
tool names, event kinds or provider names has one source; the others are derived
from it.

---

## 11. SOLID, as it applies here

| Principle | The test in this codebase |
|---|---|
| **Single responsibility** | Can the class's methods be split into groups that share no state? Then it is several classes (`R-SOL-01`). |
| **Open/closed** | Adding a provider, tool family, event kind, visual kind or slash command adds one registry entry — it never edits an `if`/`elif` chain. |
| **Liskov substitution** | A subclass honours the base contract and uses what it inherits; inheriting only to reuse a buffer is composition in disguise. |
| **Interface segregation** | Protocols stay narrow; no implementation accepts a parameter it must ignore. |
| **Dependency inversion** | Rule STD-DEP-2: depend on `ports`, receive implementations. |

---

## 12. Tests

**STD-TST-1 — The test tree mirrors the package tree** (`test/<package>/…`). The
one exception is `test/mcp_layer/`: a `test/mcp/` package would shadow the MCP SDK.

**STD-TST-2 — Shared fakes live in `test/support/`**, never copied between files.

**STD-TST-3 — No `sys.path` manipulation.** The package is installed editable.

**STD-TST-4 — Test through the public API.** No underscore imports.

**STD-TST-5 — Markers:** `pg` (needs Postgres), `billable` (makes a real model
call; guarded), `slow`. CI runs the hermetic subset.

**STD-TST-6 — Every new gate is negative-controlled**: it is seen failing on a
deliberate break before anything relies on it. A test never observed to fail is
not known to be a test.

**STD-TST-7 — A behaviour-preserving refactor keeps the agent fingerprint
byte-identical** (`test/core/test_agent_fingerprint.py`). The snapshot changes
only in its own commit, for a documented, intended change.

---

## 13. Scripts

**STD-SCR-1 — `scripts/` holds maintainer tooling.** Each script has a usage
docstring, exits 0 on success and non-zero on failure, does not touch
`sys.path`, and is listed in `scripts/README.md` with what it proves and when to
run it.

---

## 14. Electron app

**STD-UI-1 — Every HTTP, SSE and WebSocket call goes through `src/renderer/api/`.**

**STD-UI-2 — A component file holds one component of at most 300 lines.** Split
by responsibility: view, state hook, API call.

**STD-UI-3 — A hook without JSX is a `.js` file.**

**STD-UI-4 — Styling uses theme tokens and classes.** Inline `style={{…}}` only
for values computed at render time.

**STD-UI-5 — No dead components, and no two components with the same name.**

**STD-UI-6 — Configuration (dev URL, ports) is defined once**, in `src/main/config.js`.

**STD-UI-7 — ESLint (`react`, `react-hooks`) and Prettier pass; stateful logic
(stores, reducers, API client) has vitest tests.**

---

## 15. Delivery

**STD-DEL-1 — Small vertical commits.** One concern each, reviewable in minutes,
green on every gate.

**STD-DEL-2 — Mechanical changes stand alone.** Formatting, sorting and moves go
in their own commits; large ones are recorded in `.git-blame-ignore-revs`.

**STD-DEL-3 — Behaviour changes are `fix:` or `feat:` commits** with a test and a
`CHANGELOG.md` line — never hidden inside a refactor.

**STD-DEL-4 — Ratchets only move down.** Allowlists shrink; growing one needs a
reason in the commit message.

**STD-DEL-5 — Definition of done:** gates green, the verification recorded, the
documentation updated in the same commit.

---

## Enforcement summary

| Mechanism | Rules | Introduced |
|---|---|---|
| `scripts/verify_imports.py` | STD-IMP-3, STD-PKG-4 | W0 |
| ruff `E402`, `I001`, `F821`, `F822`, `F841`, `E741`, `F541` | STD-IMP-1, STD-IMP-4 | W1 |
| ruff `PLC0415`, `C901`, `PLR0913`, `BLE001`, `S110`, `T201` with shrinking allowlists | STD-IMP-2, §6, §8 | W1 |
| `test/architecture/test_import_layers.py` | STD-LAY-1, STD-IMP-5 | W1 |
| `test/test_ports_is_a_leaf.py` | STD-LAY-2 | existing |
| `test/core/test_agent_fingerprint.py` | STD-TST-7 | existing |
| `scripts/measure_code_health.py` | §6 sizes, STD-DOC-1, §9 | W0 |
| ESLint + Prettier + vitest in CI | §14 | W1, W7 |
| Code review against this document | everything else | now |
