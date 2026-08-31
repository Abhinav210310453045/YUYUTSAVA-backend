# 02 — SOLID Findings

Each finding is stated once here and referenced by ID elsewhere. Evidence is
file:line; measurements link back to [01-evidence-and-metrics.md](01-evidence-and-metrics.md).

**Index**

| ID | Principle | Title | Severity |
|----|-----------|-------|----------|
| [F-S01](#f-s01) | SRP | `build_daemon` is a 927-line composition root with 58 branches | P0 |
| [F-S02](#f-s02) | OCP | Adding a master agent means writing a fourth copy of a build function | P0 |
| [F-S03](#f-s03) | ISP | `Store` is a 30-method god object spanning 8 domains | P1 |
| [F-S04](#f-s04) | OCP | Backend selection is a 13× repeated `if pg_pool is not None` in the composition root | P1 |
| [F-S05](#f-s05) | DIP | Dependency types are erased to `object \| None` to break import cycles | P1 |
| [F-S06](#f-s06) | OCP | Two competing registries for "which LLM provider" | P1 |
| [F-S07](#f-s07) | LSP/DIP | `RoutedStore` is a typeless proxy, applied to 3 of 17 stores | P1 |
| [F-S08](#f-s08) | DIP | Five stores are reached through global mutable singletons | P1 |
| [F-S09](#f-s09) | SRP | `core/config.py` is a god module: 13 provider dataclasses + 5 unrelated config domains | P1 |
| [F-S10](#f-s10) | LSP | Store twins have divergent transaction and failure semantics | P1 |
| [F-S11](#f-s11) | SRP | `converse` is a 740-line endpoint at cyclomatic ~116 | P1 |
| [F-S12](#f-s12) | ISP | Interfaces are sized to their single implementation, not to their consumers | P2 |

---

## F-S01

### `build_daemon` is a 927-line composition root with 58 branches

**Principle:** Single Responsibility (and, consequently, Open/Closed).
**Severity:** **P0**
**Location:** `yuyutsava/daemon/bootstrap.py:291`

#### Claim

The entire daemon is composed inside one 927-line `async def` containing 58
branch points. It is simultaneously the DI container, the migration runner, the
feature-flag evaluator, the backend selector, the agent factory, and the
subsystem lifecycle owner.

#### Evidence

- `build_daemon` spans lines 291–1217 of a 1217-line module — the function *is*
  the module. (M2)
- 58 branch nodes inside the function body. (M2)
- 13 `pg_pool is not None` decisions inline (see `F-S04`).
- 17 imports deferred into the function body to manage load order. (M4)
- The module has exactly one other public surface: two frozen dataclasses
  (`DaemonOptions:118`, `DaemonSubsystems:138`) and three small config helpers.

#### Consequence

A concrete scenario. You want to add a Redis-backed cache subsystem:

1. There is no seam to add it to — you insert lines into `build_daemon` at
   whatever position the load order happens to permit.
2. You cannot unit-test the addition. The function has one entry point and
   builds *everything*; testing your 20 lines means constructing an entire
   daemon, including a Postgres pool, a LangGraph host, and an agent graph.
3. You cannot know what you broke. There are 58 branches; the flag combinations
   that reach your insertion point are not enumerable by reading.
4. Two developers adding two subsystems in the same week will conflict in the
   same function, every time.

The deeper cost is that this function is the **only** place that knows how the
system fits together. That knowledge is not expressed as structure — it is
expressed as *statement order inside one function body*, which no tool can
verify and no reader can hold in their head.

#### Why the current design is understandable

Composition roots legitimately need to know about everything; that is their job.
The defect is not that `build_daemon` has broad knowledge — it is that the
knowledge is undifferentiated. A composition root should be a *sequence of
named subsystem builders*, each independently testable. Here it is one
flat statement stream.

#### Direction

Split into subsystem builders with explicit inputs and outputs — see
[ADR-003](adr/ADR-003-composition-root-modules.md). This is only tractable
*after* `F-S04` and `F-D01` remove most of the branching, which is why it is
Phase 3 in the plan.

---

## F-S02

### Adding a master agent means writing a fourth copy of a build function

**Principle:** Open/Closed.
**Severity:** **P0**
**Location:** `yuyutsava/core/engine.py:431` (`build_cli_deepagent`), `:759`
(`build_orchestrator`), `:1003` (`build_tinker_agent`)

#### Claim

There are three master-agent builders. They are structurally the same function
with different parameter lists and small policy differences. There is no
mechanism to add a fourth without copying one — the system is closed to
extension and open to modification, exactly inverted.

#### Evidence

All three perform the same nine steps, in the same order:

| Step | `build_cli_deepagent` | `build_orchestrator` | `build_tinker_agent` |
|------|----------------------|---------------------|---------------------|
| 1. Build chat model | `:516` | caller-supplied | `:1066` |
| 2. Default checkpointer | `:517` | `:997` | `:1067` |
| 3. Base middleware list | `:522-530` | `:938-950` | `:1073-1077` |
| 4. Context middleware | `:531-541` | `:951` (`_ctx_mw`) | `:1078-1087` |
| 5. Budget + usage | `:546-557` | `:952-953` | `:1090-1100` |
| 6. Retrieval injectors | `:564-584` | `:960-978` | `:1108-1131` |
| 7. Context/domain tools | `:586-629` | `:826-849` | `:1133-1170` |
| 8. Subagent specs | `:634-670` | `:886-916` | `:1174-1195` |
| 9. `create_deep_agent(...)` | `:702`, `:732` | `:992` | `:1221` |

Parameter counts: **32**, 8, **30**. (M2)

The injector-wiring block (step 6) is a near-verbatim triplicate:

```python
# core/engine.py:564-584 (cli)     :960-978 (orchestrator)     :1108-1131 (tinker)
_injectors: list = []
if memory_store is not None:
    from yuyutsava.context.injector import MemoryInjector
    _injectors.append(MemoryInjector(memory_store))
if skill_store is not None:
    from yuyutsava.skills.injector import SkillInjector
    _injectors.append(SkillInjector(skill_store, agent=...))
if transcript_index is not None:
    from yuyutsava.context.conversation_injector import ConversationInjector
    _injectors.append(ConversationInjector(transcript_index))
...
if _injectors:
    middleware.append(RetrievalInjectionMiddleware(_injectors))
```

The async-subagent wiring block (step 8) is likewise triplicated, including its
error message, which differs only in the function name it quotes
(`:646`, `:903`, `:1181`).

#### Consequence

The divergence is already measurable, and it is silent:

| Capability | CLI | Orchestrator | Tinker |
|------------|-----|--------------|--------|
| `PrefsInjector` | ✅ `:578` | ❌ | ✅ `:1126` |
| `TodoNoteInjector` | ❌ | ❌ | ✅ `:1121` |
| `SubagentGateMiddleware` | ✅ `:529` | ✅ `:944` | ❌ |
| `FilesystemPromptOverrideMiddleware` | ✅ `:524` | ❌ | ✅ `:1074` |
| `remote_async_subagents` | ✅ `:445` | ✅ `:919` | ❌ |
| `PermissionMiddleware` | optional `:558` | ❌ never | optional `:1103` |
| Docker execution mode | ✅ `:684` | ❌ | ❌ |

**None of this matrix is anywhere in the code.** It is an emergent property of
three functions drifting. A reader cannot answer "does the tinker agent enforce
the subagent gate?" without diffing 250-line functions. `SubagentGateMiddleware`
is described at `:526-528` as enforcing the current toggle *"per call"* because
the bundle is cached — a correctness argument that applies equally to the tinker
bundle, which does not have it.

Now add a fourth master (say, a Telegram-channel agent). You copy ~250 lines,
inherit whichever subset of the matrix your source function happened to have,
and the matrix silently grows a fourth column.

#### Direction

Replace the three functions with a declarative `AgentSpec` + a single
composition pipeline — see [ADR-001](adr/ADR-001-agent-build-pipeline.md). The
capability matrix above becomes *data*, which means it becomes reviewable,
diffable, and testable.

---

## F-S03

### `Store` is a 30-method god object spanning 8 domains

**Principle:** Interface Segregation.
**Severity:** **P1**
**Location:** `yuyutsava/storage/events/store.py:51`

#### Claim

`Store` exposes ~30 public methods covering eight unrelated concerns behind one
type, and 14 modules — including agent implementations — depend on the whole
thing.

#### Evidence

Concerns on one class (`store.py:134-256`):

| Concern | Methods |
|---------|---------|
| Event payloads | `put_event_payload`, `get_event_payload`, `delete_event_payloads_with_blob_prefix`, `delete_event_payloads_older_than` |
| Proposals | `put_proposal`, `get_proposal`, `try_set_proposal_status` |
| Pending asks | `put_pending_ask`, `resolve_pending_ask`, `list_pending_asks`, `get_pending_ask` |
| Decisions | `put_decision`, `list_decisions`, `recall` |
| Consent rules | `put_consent_rule`, `list_consent_rules` |
| Consent grants | `put_consent_grant`, `delete_consent_grant`, `list_consent_grants` |
| Tool counters | `incr_tool_call`, `get_tool_call_count` |
| Preferences | `put_pref`, `delete_pref`, `get_pref`, `list_prefs` |
| Lifecycle | `start`, `stop`, `for_backend`, `sqlite_backend` |

14 importing modules (M4), including `agents/face_watcher/agent.py`,
`agents/file_organizer/agent.py`, `agents/orchestrator/spawn.py`,
`cli/commands/prefs.py`.

Note that per-domain ABCs **already exist** at `storage/events/abc.py`
(`EventStore`, `ProposalStore`, `DecisionStore`, `ConsentRuleStore`,
`ConsentGrantStore`, `ToolCounterStore`, `PendingAskStore`). `Store` is a facade
that re-aggregates the segregated interfaces back into one.

#### Consequence

- `agents/file_organizer/agent.py` needs to record a decision. It receives a
  handle to preferences, consent grants, and tool counters as well. Its test
  doubles must satisfy 30 methods to exercise one.
- The type gives no information. `store: Store` in a signature tells a reader
  nothing about what the function touches; they must read the body.
- A change to consent-grant storage has 14 modules in its blast radius by type,
  though only 2 use it. Nothing in the type system distinguishes them.
- `PrefsStore` at `storage/prefs.py` already wraps `Store` to expose only the
  preference methods — proving the segregation is wanted, and applied once.

#### Direction

The ABCs exist. Change consumer *signatures* to the narrow interface they
actually use (`store: DecisionStore`). `Store` can survive unchanged as a
construction-time aggregate; only the declared dependency types move. This is a
low-risk, incremental, high-clarity change — Phase 2 in the plan.

---

## F-S04

### Backend selection is a 13× repeated conditional in the composition root

**Principle:** Open/Closed (and DIP).
**Severity:** **P1**
**Location:** `yuyutsava/daemon/bootstrap.py:350-443`

#### Claim

The choice "Postgres or SQLite" is re-decided independently for every store,
inline, in the composition root — instead of once, behind a factory.

#### Evidence

```python
# bootstrap.py — the pattern, 13 times
artifact_store  = PgArtifactStore(...)      if pg else SqliteArtifactStore(state_db_path())   # :350/:365
summary_store   = PgThreadSummaryStore(pg)  if pg else SqliteThreadSummaryStore(...)          # :355/:366
transcript_store= PgTranscriptStore(pg)     if pg else SqliteTranscriptStore(...)             # :356/:367
voice_store     = PgVoiceMessageStore(pg)   if pg else SqliteVoiceMessageStore(...)           # :357/:368
task_store      = PgTaskStore(pg)           if pg else SqliteTaskStore(...)                   # :371-374
usage_store     = PgUsageStore(pg)          if pg else SqliteUsageStore(...)                  # :378-380
memory_store    = PgMemoryStore(...)        if pg else SqliteMemoryStore(...)                 # :392/:399
skill_store     = PgSkillStore(...)         if pg else SqliteSkillStore(...)                  # :656/:658
```

Three stores use a *different* rule again — `RoutedStore(primary, buffer, health)`
(`:428`, `:432`, `:441`) — so there are two competing selection policies in the
same function, 80 lines apart (see `F-S07`).

#### Consequence

- Adding an 18th domain requires editing `build_daemon`. The composition root is
  not open to extension.
- The two policies mean *"which stores fail over to SQLite when Postgres dies?"*
  has the answer "visuals, feedback, and todos — and no other" — a fact that
  exists nowhere except as the difference between two code shapes in one long
  function.
- A future third backend (say, an in-memory store for tests) requires editing 13
  sites, each of which must be found by reading.

#### Direction

One `StoreFactory` that resolves the backend once and produces every store
uniformly, with spillover as a decorator applied by policy, not by hand. See
[ADR-002](adr/ADR-002-storage-mapper-layer.md).

---

## F-S05

### Dependency types are erased to `object | None` to break import cycles

**Principle:** Dependency Inversion.
**Severity:** **P1**
**Location:** `yuyutsava/agents/orchestrator/agent.py:32-95`,
`yuyutsava/agents/base_sub_agent.py:60-82`

#### Claim

The system's two central dependency contracts declare roughly ten of their
fields as `object | None`, with the real type written in a comment. The stated
reason is import-cycle avoidance. DIP is being *simulated in prose* because the
module graph will not permit it in types.

#### Evidence

`OrchestratorDeps` — a 20+ field dataclass:

```python
cap_enforcer:      object | None = None   # tools.search._CapEnforcer
async_task_mirror: object | None = None   # AsyncTaskMirror
artifact_store:    object | None = None   # context.artifacts.ArtifactStore
summary_store:     object | None = None   # context.summary_store.ThreadSummaryStore
memory_store:      object | None = None   # memory.store.MemoryStore
transcript_store:  object | None = None
transcript_index:  object | None = None   # context.transcript_index.PgTranscriptIndex
context_settings:  object | None = None   # context.config.ContextSettings
remote_async_subagents: list[object] | None = None
```

`BaseSubAgent.__init__` (`base_sub_agent.py:71-73`):

```python
cap_enforcer: object | None = None,  # tools.search._CapEnforcer; untyped to avoid cycle
memory_store: object | None = None,  # memory.store.MemoryStore; untyped to avoid cycle
skill_store:  object | None = None,  # for dual-write indexing
```

Corroborating measurements (M4): 220 function-local imports (23.7% of internal
imports), 68 of them in `core/engine.py` alone; 15 comments explicitly naming
cycles; `core/engine.py` uses `Any` in 48 signature positions.

The contract is not trusted even by its own consumer — `build_orchestrator`
reads declared fields defensively:

```python
# core/engine.py:909, 919, 940, 953 …
async_subagents = getattr(deps, "async_subagents", None) or []
async_host_url  = getattr(deps, "async_host_url", None)
```

`async_subagents` **is** a declared field (`agent.py:62`). The `getattr` guard
protects against nothing the type system permits.

#### Consequence

- No static verification at the system's most important seam. Passing a
  `SummaryStore` where a `MemoryStore` belongs type-checks cleanly and fails at
  runtime, deep inside a middleware.
- IDE navigation dies exactly where a newcomer needs it most. "What can I pass
  as `memory_store`?" is answerable only by reading a comment and trusting it.
- The comments are unverifiable claims that will drift. Nothing fails when
  `# memory.store.MemoryStore` becomes wrong.
- 220 deferred imports mean import order is load-bearing behavior. The system
  works partly because of *where statements sit inside function bodies*.

The root cause is not laziness — it is a **layering inversion**. `core/` imports
from `context/`, `memory/`, `skills/`, `daemon/`, and `agents/`, while all of
those import from `core/`. There is no acyclic direction to declare types in.

#### Direction

Extract a dependency-free `yuyutsava/ports/` package holding *only* the abstract
protocols (`MemoryStore`, `ArtifactStore`, `SummaryStore`, …). Both sides import
`ports`; neither imports the other. The cycles disappear structurally, the
`object | None` fields become real types, and the 220 deferred imports mostly
collapse. See [ADR-004](adr/ADR-004-framework-boundary.md), which shares this
package.

---

## F-S06

### Two competing registries decide "which LLM provider"

**Principle:** Open/Closed.
**Severity:** **P1**
**Location:** `yuyutsava/core/config.py:510-549` vs
`yuyutsava/llm/providers/__init__.py:27-47`

#### Claim

The provider concept has a clean registry in `llm/providers/` and a hand-written
13-branch `if`-chain in `core/config.py`. Adding a provider requires editing both.

#### Evidence

The good half — `llm/providers/__init__.py`:

```python
_PROVIDERS: tuple[Provider, ...] = (
    AnthropicProvider(), GoogleProvider(), VertexProvider(),
    BedrockProvider(), AzureOpenAIProvider(), MistralProvider(), CohereProvider(),
)
_FALLBACK: Provider = OpenAICompatibleProvider()

def provider_for(settings: LlmSettings) -> Provider:
    for provider in _PROVIDERS:
        if provider.settings_type is not None and isinstance(settings, provider.settings_type):
            return provider
    return _FALLBACK
```

Its own docstring: *"Adding a provider is a new module here plus one line in
`_PROVIDERS`. Nothing else in the system needs to know it exists."*

The contradicting half — `core/config.py:510`:

```python
def llm_settings_from_env(role: str | None = None) -> LlmSettings:
    provider = _env("LLM_PROVIDER", effective_role, "groq").lower()
    if provider == "groq":        return GroqSettings.from_env(effective_role)
    if provider == "openrouter":  return OpenRouterSettings.from_env(effective_role)
    if provider == "ollama":      return OllamaSettings.from_env(effective_role)
    if provider == "openai":      return OpenAISettings.from_env(effective_role)
    if provider in ("openai_compatible", "custom"): return OpenAICompatibleSettings.from_env(...)
    if provider == "anthropic":   return AnthropicSettings.from_env(effective_role)
    if provider in ("google", "gemini"): return GoogleSettings.from_env(effective_role)
    if provider == "vertex":      return VertexSettings.from_env(effective_role)
    if provider in ("bedrock", "aws"):   return BedrockSettings.from_env(effective_role)
    if provider in ("azure", "azure_openai"): return AzureOpenAISettings.from_env(...)
    if provider == "mistral":     return MistralSettings.from_env(effective_role)
    if provider == "cohere":      return CohereSettings.from_env(effective_role)
    raise RuntimeError(f"Unknown LLM_PROVIDER={provider!r}; use one of: groq, openrouter, ...")
```

Plus the 13 settings dataclasses themselves, at `config.py:99-509`.

#### Consequence

The docstring's promise is false. Adding a provider is: (1) a settings dataclass
in `core/config.py`, (2) a branch in the `if`-chain, (3) the provider module,
(4) a `_PROVIDERS` entry — and the error message at `:546-549` is a
*hand-maintained fourth list* of provider names that will drift from the other
three.

`core/config.py` has 42 importers (M4), so a provider addition dirties the
highest-fan-in module in the system, invalidating its import cache for
everything.

#### Consequence in miniature

The single most damaging effect of `F-S06` is not the edit cost — it is that a
developer reading `llm/providers/__init__.py` will *believe the docstring*, add
their module and tuple entry, and find their provider unreachable because
`LLM_PROVIDER=myprovider` falls through to the `RuntimeError`.

#### Direction

Move the env→settings mapping onto `Provider` itself (`Provider.env_names`,
`Provider.settings_from_env()`), and derive both the dispatch and the error
message from `_PROVIDERS`. `llm_settings_from_env` becomes a lookup. The 13
dataclasses move to `llm/providers/<name>.py` beside the provider that owns
them, which also shrinks the 42-importer god module (`F-S09`).

---

## F-S07

### `RoutedStore` is a typeless proxy, applied to 3 of 17 stores

**Principle:** Liskov Substitution / Dependency Inversion.
**Severity:** **P1**
**Location:** `yuyutsava/storage/routing/facade.py:26-54`,
wired at `yuyutsava/daemon/bootstrap.py:428-443`

#### Claim

The spillover proxy is a good idea implemented in a way that defeats static
typing, and it is applied to a small, undocumented subset of stores — so
"survives a Postgres outage" is an unstated per-store property.

#### Evidence — the typing problem

```python
class RoutedStore:
    def __init__(self, primary: Any, buffer: Any, health: StorageHealth, *, name: str = "") -> None: ...
    def __getattr__(self, attr: str) -> Any:
        ...
        return _wrapped   # dynamically synthesized
```

`RoutedStore` does not inherit from `TodoStore`, `VisualStore`, or
`FeedbackStore`. It is substitutable at runtime and invisible to the type
checker:

- `isinstance(store, TodoStore)` → `False`. Any such guard silently misbehaves.
- Every method resolves to `Any`. Calling `store.add_crad(...)` (typo)
  type-checks fine and raises `AttributeError` at runtime.
- Non-coroutine attributes are returned unwrapped (`facade.py:39-41`), so a
  store that mixes sync and async methods has *partial* failover — with no
  declaration of which methods are protected.

#### Evidence — the application problem

Docstring (`facade.py:10-11`): *"Generic by design (`__getattr__`) so it works
for every domain twin — events, consent, interrupts, memory, skills — without
per-method boilerplate."*

Actual wiring (`bootstrap.py:428-443`): `PgVisualStore`, `PgFeedbackStore`,
`PgTodoStore`. **Not** events, **not** consent, **not** interrupts, **not**
memory, **not** skills — every domain the docstring names is excluded.

The other 14 stores use the bare conditional of `F-S04` and therefore have **no
failover at all**: if Postgres dies mid-session, a `PgMemoryStore` write raises
into agent code.

#### Consequence

Two failure modes coexist under one apparent architecture. During a Postgres
outage, a todo write silently lands in SQLite and reconciles later, while a
memory write from the same agent turn raises. Nothing in the code declares this
split; it is only visible by comparing two wiring styles 80 lines apart in a
927-line function.

#### Direction

Make the proxy typed and the policy explicit — see
[ADR-002](adr/ADR-002-storage-mapper-layer.md). Either generate typed proxies per
interface, or declare failover as a per-store policy flag consumed by a single
factory, so *which stores fail over* is data rather than an accident of wiring.

---

## F-S08

### Five stores are reached through global mutable singletons

**Principle:** Dependency Inversion.
**Severity:** **P1**
**Location:** `visuals/store.py:319-330`, `todoboard/store.py:1098-1110`,
`storage/feedback_store.py:295-306`, `storage/sessions/sqlite_impl.py:349-362`,
`todoboard/recall.py:269-280`

#### Claim

Five stores plus two policy objects are installed into module-level globals at
boot and fetched by consumers through `get_default_*()` — a service locator.
Dependencies are hidden from signatures and from tests.

#### Evidence

```python
# the pattern, five times
_default_store: VisualStore | None = None
def set_default_visual_store(store: VisualStore) -> None: ...
def get_default_visual_store() -> VisualStore: ...
```

Also `set_default_policy` / `set_default_consent` at
`agents/task_runner/tools.py:145-190`, described in the source as *"set by
daemon at boot"*.

`storage/purge.py` is built entirely on this: *"Self-contained: reads the
process-singleton session store … the CLI and the web endpoint call it
identically with no store wiring"* (`purge.py:113-115`).

#### Consequence

- **Hidden dependencies.** `purge_session(session_id)` has a one-argument
  signature and touches four stores. Nothing at the call site says so.
- **Test isolation is manual.** Every test that touches these paths must set and
  restore globals. Forgetting leaks state between tests in the same process.
- **Boot order is load-bearing and unenforced.** A `get_default_*` before its
  `set_default_*` raises at runtime. Nothing in the type system prevents it.
- **Single instance per process.** Two agents cannot use different todo stores.
  Not needed today; it forecloses multi-tenancy structurally.

The convenience is real — `purge_session` genuinely is nicer to call this way.
The cost is that the convenience is purchased with global mutable state at the
system's persistence boundary.

#### Direction

Keep the ergonomics, remove the globals: a single explicit `AppContext` /
`Subsystems` handle threaded through call sites. `purge_session(ctx, session_id)`
is barely less convenient and is honest about what it touches. Do this as part of
Phase 3, when `build_daemon` is already being restructured.

---

## F-S09

### `core/config.py` is a god module

**Principle:** Single Responsibility.
**Severity:** **P1**
**Location:** `yuyutsava/core/config.py` (816 lines, **42 importers**)

#### Claim

The highest-fan-in module in the system holds six unrelated concerns.

#### Evidence

| Lines | Concern |
|-------|---------|
| 88–509 | 13 LLM provider settings dataclasses + the `LlmSettings` Protocol |
| 510–549 | Provider selection from env (`F-S06`) |
| 565–595 | `LimitsConfig` — content size caps |
| 596–618 | `TimingConfig` — timeouts |
| 619–722 | `SourceConfig` / `EventsConfig` — the event system |
| 723–791 | `DaemonConfig` |
| 792–816 | `SearchConfig` |
| 148–210 | `DockerSettings`, `LocalSettings` — execution backends |

#### Consequence

- Any change to any config concern invalidates the import cache for 42 modules.
- A module needing only `SearchConfig` transitively pulls the definitions of 13
  provider dataclasses.
- It is a magnet for cycles: it is imported by nearly everything, so anything it
  imports becomes un-importable by nearly everything — which is a direct
  contributor to the deferred-import problem in `F-S05`.

The project has already recognized this once: *"Path functions moved to
`yuyutsava.storage.paths` (Step 1 of restructure)"* (`config.py:552-555`). The
restructure was started and stopped.

#### Direction

Finish the started restructure. Provider settings move to `llm/providers/`
(also fixing `F-S06`); `EventsConfig` moves to `events/`; `DaemonConfig` moves to
`daemon/`. What remains is a genuine leaf: limits, timing, search.

---

## F-S10

### Store twins have divergent transaction and failure semantics

**Principle:** Liskov Substitution.
**Severity:** **P1**
**Location:** e.g. `yuyutsava/todoboard/store.py:470-497` vs `:904-930`

#### Claim

The SQLite and Postgres implementations of the same interface do not behave
identically under concurrency and failure. Substituting one for the other
changes program semantics, which is precisely what LSP forbids.

#### Evidence

`SqliteTodoStore.assign_note` (`:470`) runs through
`BaseSqliteStore._run_write` (`storage/base.py:170-198`), which provides:

- `BEGIN IMMEDIATE` — an explicit write transaction,
- a per-process `asyncio.Lock` serializing writers,
- retry-on-`SQLITE_BUSY`, up to 3 attempts with backoff,
- commit on success, rollback on exception.

`PgTodoStore.assign_note` (`:904`) runs `async with self._pool.connection()`,
with none of the above: no explicit transaction wrapper, no retry policy, no
serialization.

The methods also differ in *shape*: SQLite performs `UPDATE` → `SELECT` →
`UPDATE parent`; Postgres performs `UPDATE … RETURNING card_id` → `UPDATE
parent` → `SELECT`. Different statement counts, different intermediate states
visible to a concurrent reader.

#### Consequence

- A concurrency bug reproducible on SQLite may be unreproducible on Postgres,
  and vice versa. The dev machine and production do not agree on semantics.
- `RoutedStore` (`F-S07`) *switches between these two at runtime, mid-session*,
  on a Postgres error. A single logical session can therefore span two different
  transaction models, silently.
- Every future twin author must independently rediscover that the two sides need
  matching atomicity — nothing enforces it.

#### Direction

Collapsing the twins (`F-D02`, [ADR-002](adr/ADR-002-storage-mapper-layer.md))
dissolves this finding: with one implementation over two dialect adapters, there
is one transaction policy by construction. Until then, the honest interim fix is
to document the divergence and add a shared conformance test suite that both
implementations must pass — see the Phase 0 test harness in the plan.

---

## F-S11

### `converse` is a 740-line endpoint at cyclomatic ~116

**Principle:** Single Responsibility.
**Severity:** **P1**
**Location:** `yuyutsava/daemon/web/routers/converse.py:311`

#### Claim

The highest-complexity function in the codebase is an HTTP/WebSocket handler
holding transport, audio pipeline, agent streaming, interrupt handling, and
persistence in one body.

#### Evidence

740 lines, 116 branch nodes (M2) — more than twice the next-highest branch count
outside `build_daemon`. The module is 1050 lines, so the endpoint is 70% of it.
A nested helper `_run_voice_turn` (`:613`) is itself 116 lines at cx=24.

The `services/` and `repositories/` directories exist alongside it —
`repositories/` is **empty** (M6). The layering this router was meant to sit on
top of was never built.

#### Consequence

- 116 branches is beyond exhaustive reasoning. No reviewer can enumerate the
  paths; no test suite plausibly covers them.
- Reuse is impossible. The CLI needs the same conversation logic and therefore
  has its own copy of the flow (`F-D03`).
- Every change to voice, streaming, interrupts, or persistence lands in the same
  function, so those four concerns cannot be worked on independently.

#### Direction

Extract a `ConversationTurn` service that owns the turn lifecycle, leaving the
router to do transport only. A `conversation/service.py` already exists (316
lines) — the extraction target is partly present and simply bypassed by this
route. Fill in `repositories/` or delete it; an empty layer is a false signal.

---

## F-S12

### Interfaces are sized to their implementation, not to their consumers

**Principle:** Interface Segregation.
**Severity:** **P2**
**Location:** `yuyutsava/todoboard/store.py:47-157` and peers

#### Claim

The domain ABCs enumerate every operation the implementation happens to provide,
rather than the cohesive role a consumer needs. `TodoStore` declares 21 abstract
methods across four entity types.

#### Evidence

`TodoStore` (`:47-157`) covers cards (`add_card`, `get_card`, `update_card`,
`delete_card`, `list_cards`, `list_card_ids`), objectives (`add_objective`,
`get_objective`, `update_objective`, `delete_objective`, `assign_note`), events
(`add_event`, `list_events`), notes (`add_note`, `update_note`, `delete_note`),
and attachments (`add_attachment`, `update_attachment`, `delete_attachment`,
`list_all_attachments`).

The capture-scope tool set (`todoboard/tools.py`, `scope="capture"`) uses three
of the 21: `add_card`, `list_cards`, `get_card`.

#### Consequence

Every implementation, test double, and future backend must supply 21 methods
regardless of use. This is the mechanism by which `F-D02` becomes expensive: the
per-domain cost of a second backend is proportional to interface width, and the
interfaces are as wide as they can be.

#### Note on severity

P2 rather than P1 because `TodoExchange` (`todoboard/exchange.py`) already acts
as the segregating layer above the store for most consumers, and the memory notes
record it as the intended sole access path. The finding is real but partly
mitigated in practice. It matters mainly as a **multiplier on `F-D02`** — halving
interface width roughly halves the cost of the twin problem.
