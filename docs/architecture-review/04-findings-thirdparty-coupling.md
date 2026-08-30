# 04 — Third-Party Coupling & the Abstraction Ceiling

*The second requested analysis: how dependent is this design on abstractions
supplied by third-party frameworks, and where does our own design stop and
theirs begin?*

**Index**

| ID | Title | Severity |
|----|-------|----------|
| [F-T01](#f-t01) | The cross-cutting-concern architecture *is* LangChain's `AgentMiddleware` | P0 |
| [F-T02](#f-t02) | `BaseChatModel` is the system's universal currency | P1 |
| [F-T03](#f-t03) | `create_deep_agent` is the only way this system knows how to build an agent | P1 |
| [F-T04](#f-t04) | Load-bearing dependencies on `deepagents` *internals*, with no version ceiling | P0 |
| [F-T05](#f-t05) | Zero upper version bounds across 11 framework dependencies | P0 |
| [F-T06](#f-t06) | LangGraph's `interrupt()` is called from inside domain code | P1 |
| [F-T07](#f-t07) | Middleware imports target a submodule path, and hook-API churn is already visible | P1 |

---

## The headline

> **We do not depend on third-party libraries. We are built *inside* one.**

The distinction matters. A dependency is something you call. A framework is
something that calls you — it owns the control flow, defines the extension
points, and dictates the shape of your own abstractions.

Every one of this system's cross-cutting concerns — permissions, budget, usage
accounting, context compaction, tool filtering, transcript recording, retrieval
injection, voice styling, subagent gating, background-task capping — is
implemented as a subclass of a LangChain base class, invoked by a LangGraph
graph, constructed by a `deepagents` factory. There is no seam anywhere in that
chain that belongs to YUYUTSAVA.

**Installed versions at review time:**

```
deepagents      0.6.3    (declared floor: >=0.6.3 — running exactly at the floor)
langchain       1.3.1
langchain-core  1.4.8
langgraph       1.2.1
langchain-openai 1.1.11  (declared floor: >=1.1.11)
```

---

## Coupling map: where our design stops

| Layer | Whose abstraction? | Assessment |
|-------|--------------------|------------|
| Provider selection | **Ours** (`llm/providers/`) | ✅ Clean registry, real seam |
| Chat model object | **Theirs** (`BaseChatModel`) | ❌ `F-T02` |
| Agent graph construction | **Theirs** (`create_deep_agent`) | ❌ `F-T03` |
| Cross-cutting concerns | **Theirs** (`AgentMiddleware`) | ❌ `F-T01` |
| Agent state | **Theirs** (`AgentState`) | ❌ |
| Tool definition | **Theirs** (`langchain_core.tools`, 23 modules) | ❌ |
| Message types | **Theirs** (`langchain_core.messages`, 20 modules) | ❌ |
| Human-in-the-loop | **Theirs** (`langgraph.types.interrupt`) | ❌ `F-T06` |
| Checkpointing | **Theirs** (`BaseCheckpointSaver`) | ❌ |
| Filesystem / shell backend | **Theirs** (`deepagents.backends`) | ❌ |
| Subagent definition | **Ours** (`BaseSubAgent`) → **their** spec dict | ⚠️ Adapter exists, output is theirs |
| Retrieval / vector search | **Ours** (`yuyutsava/retrieval/`) | ✅ Clean |
| OS abstraction | **Ours** (`yuyutsava/platform/`) | ✅ Clean |
| Persistence | **Ours** (17 ABCs) | ✅ Clean (its problems are internal — `F-D02`) |

**Reading the map:** the boundary is drawn in exactly the right place for
*storage*, *OS*, and *retrieval* — and nowhere at all for *agents*. The team
demonstrably knows how to draw a boundary; it has not drawn one around the
framework.

---

## F-T01

### The cross-cutting-concern architecture *is* LangChain's `AgentMiddleware`

**Severity:** **P0**
**Location:** 14 classes; enumerated in [01 § M5](01-evidence-and-metrics.md#m5--third-party-surface-area)

#### Claim

Fourteen classes implementing YUYUTSAVA's own policies extend
`langchain.agents.middleware.AgentMiddleware` directly. There is no intermediate
base class, protocol, or adapter. Our policy layer has no independent existence.

#### Evidence

All 14 subclass the framework type directly:

`PermissionMiddleware`, `BudgetMiddleware`, `UsageRecorder`,
`ToolFilterMiddleware`, `ToolResultOffloadMiddleware`, `SubagentGateMiddleware`,
`VoiceStyleMiddleware`, `RetrievalInjectionMiddleware`,
`FilesystemPromptOverrideMiddleware`, `TranscriptRecorderMiddleware`,
`PromptInspectorMiddleware`, `BackgroundTaskCapMiddleware`,
`CheckAsyncTaskGuardMiddleware`, `AsyncTaskInterruptPatchMiddleware`.

They also consume the framework's *data* types in their signatures —
`AgentState`, `ModelRequest`, `ModelResponse`, `ToolCallRequest` — so even the
method bodies are framework-shaped.

`PermissionMiddleware` carries `# type: ignore[misc]` on its class statement
(`core/permission_middleware.py:254`): the base class contract does not quite fit
what we need, and the mismatch is suppressed rather than adapted.

#### Consequence

**Nothing in this list is a LangChain concern.** Budget ceilings, cost
accounting, consent policy, transcript persistence, and skill retrieval are
YUYUTSAVA's product. They are expressed exclusively in a vocabulary owned by
someone else.

- **Untestable without the framework.** Testing `BudgetMiddleware` requires
  constructing an `AgentState` and a `ModelRequest`. Every policy test carries the
  cost of a heavy framework import — which the project has already recognized as a
  problem and works around by avoiding those tests.
- **A hook-signature change is a 14-file change.** Not a 14-line change: the
  hook signature determines what state each policy can see.
- **Policies cannot run anywhere else.** If a second execution path ever exists —
  a lightweight non-graph runner, a batch job, a different orchestration library —
  every policy must be rewritten.
- **No policy inventory exists.** There is no type, registry, or list that
  enumerates "the policies YUYUTSAVA enforces". They are discoverable only by
  grepping for a third-party base class.

#### Direction

A thin `yuyutsava/policy/` layer: our own `Policy` protocol expressed in our own
types, plus one generic `LangChainPolicyAdapter(AgentMiddleware)` that maps the
framework's hooks onto it. 14 framework subclasses become 1. Policies become
plain testable objects. See [ADR-004](adr/ADR-004-framework-boundary.md).

This is Phase 4 — the largest change here, and deliberately last. `F-T05`'s
version ceilings cap the risk in the meantime at a fraction of the cost.

---

## F-T02

### `BaseChatModel` is the system's universal currency

**Severity:** **P1**
**Location:** 18 modules; `llm/factory.py:44` is the origin

#### Claim

The `llm/` package is an excellent **construction** abstraction and not at all an
**insulation** abstraction. It controls *how a model is built* and then hands
back a raw framework object that flows everywhere.

#### Evidence

```python
# llm/base.py:59 — the abstract method every provider implements
@abstractmethod
def build(self, settings, *, temperature, disable_reasoning) -> BaseChatModel: ...
```

The return type is the framework's. From there `BaseChatModel` appears in 18
modules, including `core/engine.py` (10 references), `core/model_router.py` (7),
`agents/base_sub_agent.py` (4), `daemon/orchestrator_loop.py` (3),
`context/compaction.py` (2), `agents/triage/agent.py` (2).

`model_name_of` (`llm/factory.py:22-38`) is the tell:

```python
for attr in ("model_name", "model", "model_id"):
    v = getattr(model, attr, None)
    if isinstance(v, str) and v:
        return v
return ""
```

Because the abstraction returns an opaque framework object, the system must
**duck-type across six vendor SDK classes** to recover a fact — the model's name
— that the `Provider` knew for certain at construction time and threw away. The
docstring concedes the point: *"A per-provider accessor on `Provider` is the
natural home once something needs it to be exact rather than best-effort."*

#### Consequence

- The `Provider` seam is one function wide. Everything downstream is coupled
  regardless.
- Capability differences leak as `getattr` probes and `try/except` rather than as
  declared interface properties.
- Consumers that need *only* "invoke this with messages, get a reply" — triage,
  compaction — receive the full LangChain runnable surface.

#### Note on proportionality

This is **P1, not P0**, and the reason is worth stating. `BaseChatModel` is a
genuinely broad, stable, well-designed interface that every LangChain provider
implements; wrapping it wholesale would be a large cost for modest benefit.

The proportionate fix is narrow: **stop returning bare framework objects from our
own factory.** A `ModelHandle` dataclass — `{model: BaseChatModel, name: str,
provider: str, capabilities: frozenset}` — costs almost nothing, deletes
`model_name_of`'s duck-typing entirely, and gives capability checks a real home.
The framework object stays accessible for the code that legitimately needs it.

---

## F-T03

### `create_deep_agent` is the only way this system knows how to build an agent

**Severity:** **P1**
**Location:** `core/engine.py:702`, `:732`, `:992`, `:1221`

#### Claim

All four graph construction sites call `deepagents.create_deep_agent`. There is
no interface describing what an agent *is* to YUYUTSAVA, independent of that
function's parameter list.

#### Evidence

Every builder ends in the same call, with the same argument shape:

```python
create_deep_agent(
    model=..., tools=..., backend=..., system_prompt=...,
    checkpointer=..., middleware=..., subagents=...,
)
```

The return type — `CompiledStateGraph` (LangGraph) — is what `AgentBundle.agent`
holds (`engine.py:195`), what `astream_agent*` consumes (`streaming.py:363`,
`:593`), and what the daemon and CLI drive.

`BaseSubAgent.as_deepagents_subagent_spec()` (`base_sub_agent.py:290`) is
correctly shaped as an adapter — but it adapts *to* the framework, converting our
type into their dict. There is no adapter in the other direction.

#### Consequence

- "What is a YUYUTSAVA agent?" has one answer: *whatever `create_deep_agent`
  returns*. The concept has no independent definition.
- The 32-parameter signature of `F-K02` exists partly because the builder must
  marshal everything `create_deep_agent` might want.
- Because `F-D03` handles the streaming protocol inline in two 226-line
  functions, the framework's runtime contract is embedded in the codebase twice
  as deeply as it needs to be.

#### Direction

Introduce a minimal `Agent` protocol (`astream(input) -> AsyncIterator[Event]`,
`ainvoke(input) -> Result`) and have the builder return something satisfying it.
The deepagents graph becomes the default implementation rather than the
definition. Bundled with [ADR-001](adr/ADR-001-agent-build-pipeline.md), since
the builder is being restructured there anyway.

---

## F-T04

### Load-bearing dependencies on `deepagents` internals, with no version ceiling

**Severity:** **P0** — the highest-risk finding in this document

#### Claim

The system depends on undocumented internal behavior of `deepagents`, in ways
that fail *silently* rather than loudly, while permitting unbounded automatic
upgrades of that library.

#### Evidence — three distinct internal dependencies

**1. A prompt-string match against a library constant.**

```python
# core/filesystem_prompt_middleware.py:43-47
try:
    from deepagents.middleware.filesystem import FILESYSTEM_SYSTEM_PROMPT as _FS_PROMPT
except Exception:
    _FS_PROMPT = None
_ANCHOR = "## Filesystem Tools"   # stable fallback marker
```

The middleware strips a prompt block by matching either the imported constant or
a hardcoded markdown heading. The `try/except` is a reasonable guard, but note
what it guards *toward*: on import failure the code falls back to string
matching, and if the library rewords the heading, `_is_fs_block` silently returns
`False` for every block.

**Failure mode:** the filesystem block is no longer stripped. No exception, no
log. Every agent silently receives instructions to use built-in filesystem tools
that `ToolFilterMiddleware` has removed. The symptom is degraded agent behavior
and wasted cache-prefix tokens, discovered days later, and it does not look like
a dependency problem.

**2. Line-numbered references to library source.**

```python
# agents/general_purpose/agent.py:85-87
# Must be exactly "general-purpose" (kebab-case) so that deepagents'
# ... .venv/.../deepagents/graph.py:240-246.
```
```python
# core/engine.py:482-484
# Passing GeneralPurposeAgent here name-match-overrides deepagents'
# built-in default (see ``deepagents.graph`` ~line 240-246)
```

Our general-purpose subagent overrides the framework's built-in **by string
name collision**, a behavior documented only by pointing at line numbers in the
installed package.

**Failure mode:** the library renames its default or changes its match logic.
Our tighter spec stops applying; the framework's permissive default silently
takes over. No error — the agent simply gets different, broader capabilities than
intended. This is a *security-adjacent* silent regression: the override exists to
constrain a subagent.

**3. Subclassing framework backend internals.**

```python
# core/docker_sandbox_backend.py:21-29
from deepagents.backends.protocol import (...)
from deepagents.backends.sandbox import BaseSandbox
```

`DockerSandboxBackend` (513 lines) subclasses `BaseSandbox` and implements a
protocol from `deepagents.backends.protocol` — an implementation submodule, not a
top-level export.

#### Evidence — no version ceiling

```
deepagents>=0.6.3     # installed: 0.6.3 — exactly at the floor
```

There is no upper bound. `uv sync` on a fresh checkout tomorrow may install
0.7.x, or 1.0. Combined with the three silent-failure modes above, this is the
sharpest risk in the codebase.

#### Consequence, stated plainly

> A routine `uv lock --upgrade` can change agent behavior — including subagent
> capability scope — with **no error, no test failure, and no log line**. The
> only signal is degraded output quality, attributed to the model rather than to
> the dependency.

Nothing currently detects any of the three failures. There is no characterization
test asserting that the filesystem block is actually stripped, or that the
name-match override actually took effect.

#### Direction — cheap, immediate, Phase 0

1. **Pin ceilings**: `deepagents>=0.6.3,<0.7`, and equivalently for `langchain`,
   `langchain-core`, `langgraph`. Hours of work.
2. **Characterization tests** — one per internal dependency, each asserting the
   *observable outcome*, not the mechanism:
   - build a CLI agent, render its system prompt, assert `"## Filesystem Tools"`
     is absent;
   - build an agent with `GeneralPurposeAgent`, assert the resolved
     `general-purpose` spec is ours (check for a marker only our spec carries);
   - assert `DockerSandboxBackend` still satisfies the backend protocol.
3. **Make failures loud**: `FilesystemPromptOverrideMiddleware` should log at
   WARNING when it strips nothing, since stripping nothing is never the intended
   outcome.

Step 2 converts three silent failures into three red tests. **This is the single
highest value-per-hour item in the entire review.**

---

## F-T05

### Zero upper version bounds across 11 framework dependencies

**Severity:** **P0**
**Location:** `pyproject.toml:6-37`, `:89-104`

#### Claim

Every dependency is declared as an open-ended floor. Two `<` characters exist in
the whole file, neither on a framework dependency.

#### Evidence

```
deepagents>=0.6.3                 langgraph-checkpoint-sqlite>=3.0.3
langgraph-cli[inmem]>=0.4.27      langgraph-checkpoint-postgres>=2.0.0
langchain-openai>=1.1.11          langchain-anthropic>=1.4
langchain-google-genai>=4.2       langchain-google-vertexai>=3.2
langchain-aws>=1.6                langchain-mistralai>=1.1
langchain-cohere>=0.6
```

#### Consequence

`uv.lock` protects existing checkouts. It does not protect: a fresh clone, CI on
a clean cache, a deliberate `--upgrade`, or any contributor who regenerates the
lock. In every one of those cases the resolver may pick a new major version of a
framework the codebase couples to at 64 of 350 modules.

The exposure is not hypothetical. LangChain's 1.x middleware API is actively
evolving, and `F-T07` shows this codebase already straddles two generations of
its hook API.

#### Direction

Add ceilings at the next major for every framework dependency; keep them tight
for `deepagents` (`<0.7`, given `F-T04`) and one major wide for the rest. Add a
scheduled CI job that resolves *without* the lock and runs the characterization
suite — so an incompatible release is discovered by a green-to-red transition on
a schedule, not by a developer mid-feature.

---

## F-T06

### LangGraph's `interrupt()` is called from inside domain code

**Severity:** **P1**
**Location:** `agents/task_runner/tools.py:509`, `core/permission_middleware.py:305`, `:324`

#### Claim

Human-in-the-loop is not a YUYUTSAVA concept with a LangGraph implementation. It
is a LangGraph mechanism invoked directly from tool and policy code.

#### Evidence

```python
# agents/task_runner/tools.py:24, 509
from langgraph.types import interrupt
...
response: str = interrupt(payload)
```

The resume protocol is handled on the other side, twice, in
`core/streaming.py` (`F-D03`), including the non-obvious multi-interrupt rule at
`:461`. `agents/task_runner/permissions.py:156` builds *"the typed interrupt
payload passed to LangGraph's `interrupt()`"` — so our own payload type is named
after their function.

#### Consequence

- HITL — a genuine product feature with consent tiers, risk gating, and a
  persistent grant store — cannot be exercised without a LangGraph runtime.
- The pause/resume contract is split across a tool, a middleware, and two
  streaming loops, coupled by a framework calling convention rather than by an
  interface.
- The consent core (`yuyutsava/consent/`) is well-factored and independent; its
  *delivery mechanism* is not. The gap between those two is where this finding
  lives.

#### Direction

An `AskUser` port: domain code calls `await ctx.ask(prompt)`; the LangGraph
adapter implements it via `interrupt()`. Naturally paired with
[ADR-004](adr/ADR-004-framework-boundary.md), and it makes consent logic
testable without a graph.

---

## F-T07

### Middleware imports target a submodule path

**Severity:** **P2** *(downgraded from P1 — see correction below)*
**Status:** ✅ **FIXED** in Phase 0 — all 16 sites now use the public path
**Location:** 16 import sites across the middleware layer

> ### ⚠️ Correction (2026-08-08, during Phase 0 execution)
>
> **This finding originally claimed the codebase straddles "two generations" of
> the middleware hook API, citing `before_model` as an older style superseded by
> `wrap_model_call`. That claim was wrong.** Both are current, first-class hooks
> on `AgentMiddleware` with *different semantics*:
>
> | Hook | Signature | Can it do what the other does? |
> |------|-----------|-------------------------------|
> | `before_model` | `(state, runtime) -> state updates` | Writes agent state |
> | `wrap_model_call` | `(request, handler) -> ModelResponse` | Wraps the call, rewrites the request |
>
> `context/compaction.py:185` uses `before_model` because compaction **replaces
> `state["messages"]`**, which `wrap_model_call` structurally cannot do.
> `context/prompt_inspector.py:71` uses it for read-only state observation.
> Neither is legacy; both are the correct hook for their job.
>
> The error was inferring deprecation from a naming pattern without checking the
> API. **Acting on it would have broken compaction** — the migration was on the
> Phase 0 checklist and was cancelled after verification.
>
> The severity drops to P2 accordingly: the import-path split was real and worth
> fixing, but there is no evidence of hook-API churn, so this is hygiene rather
> than a moving-target risk. **`F-T01`'s "a hook change is a 14-file change"
> remains true** — it is a statement about coupling, and does not depend on churn
> having already occurred.

#### Claim

Ten of the sixteen middleware imports reached past the package's public
re-export into the `langchain.agents.middleware.types` submodule, while six used
the public path — the same symbol imported two ways in one codebase.

#### Evidence — import path split

| Path | Sites |
|------|-------|
| `from langchain.agents.middleware.types import …` | **10** |
| `from langchain.agents.middleware import …` | 6 |

The same symbol (`AgentMiddleware`) is imported via two different paths in the
same codebase — e.g. `core/permission_middleware.py:29` (`.types`) vs
`daemon/budget.py:20` (public). The `.types` submodule is an implementation
module; the public package re-exports it. Reaching into the submodule is one
refactor away from breaking, and it breaks *loudly* (ImportError), which is
better than `F-T04` but still avoidable.

#### Hook usage — *not* evidence of churn (retracted)

For the record, since the original finding drew a false conclusion from it. All
three hooks below are current API, each used correctly for its purpose:

| Hook | Sites | Why this hook |
|------|-------|---------------|
| `before_model` | `context/compaction.py:185`, `context/prompt_inspector.py:71` | Returns **state updates** — compaction replaces `state["messages"]`; the inspector reads state |
| `wrap_model_call` | `subagent_gate:119`, `voice_style:86`, `tool_filter:69`, `filesystem_prompt:91`, `retrieval_injection:98` | Rewrites the outbound **request** |
| `wrap_tool_call` | `subagent_gate:172`, `check_guard:52`, `cap_middleware:51`, `interrupt_middleware:70` | Intercepts **tool** execution |

Verified against `AgentMiddleware` in langchain 1.3.1: `before_model`,
`after_model`, `wrap_model_call`, `wrap_tool_call`, `before_agent`, `after_agent`
and their `a*` variants are all present and current. There is no deprecated
generation in use.

#### Consequence

Reaching into `.types` was one upstream refactor away from an `ImportError`
across ten modules. Loud rather than silent, so lower severity than `F-T04` — but
free to avoid, and now avoided.

#### Direction — ✅ done

All 16 sites now import from the public `langchain.agents.middleware` path
(verified: the five symbols used are the *identical objects* on both paths, so
the change is a no-op at runtime). No hook migration was performed, or needed.

Long term, [ADR-004](adr/ADR-004-framework-boundary.md) would reduce the
`F-T01` blast radius from 14 modules to one — that argument stands on the
coupling itself, independent of this retraction.

---

## Summary: what breaks, and how loudly

Ordered by danger — silent failures first, because those are the ones that reach
production.

**Updated after Phase 0 execution (2026-08-08).** The "Detection" column now
reflects the tripwires that exist and have been *negative-controlled* — each was
observed to go red when its contract was deliberately broken.

| # | Failure | Trigger | Signal | Detection |
|---|---------|---------|--------|-----------|
| 1 | Filesystem prompt block stops being stripped | `deepagents` rewords a prompt | **None** → now WARN log | ✅ `test_filesystem_prompt_override.py` |
| 2 | `general-purpose` override stops applying | `deepagents` renames its default | **None** | ✅ `test_general_purpose_override_contract` |
| 3 | Subagent capability scope silently widens | as #2, or dispatch changes | **None** | ✅ + `test_general_purpose_auto_add_still_name_keyed` |
| 3b | **Non-Docker agents stop building** | **`deepagents` 0.7.0 removes callable `backend=`** | TypeError at build | ✅ `test_backend_factory_still_accepted` + pinned `<0.7` |
| 4 | Middleware hook signature change | `langchain` major release | ImportError / TypeError | ✅ loud; capped by `<2` |
| 5 | `.types` submodule reorganized | `langchain` refactor | ImportError | ✅ n/a — no longer imported |
| 6 | Backend protocol change | `deepagents` release | TypeError at construction | ✅ `test_docker_backend_satisfies_protocol` |
| 7 | Interrupt/resume protocol change | `langgraph` release | runtime error mid-turn | ⚠️ loud but late — **still uncovered** |

**Rows 1–3 were the reason `F-T04` is P0**, and they are now closed: five
tripwires exist, all negative-controlled. Actual cost was well under the
estimated day.

**Row 3b was discovered *during* Phase 0 and did not appear in the original
review.** deepagents emits a `DeprecationWarning` stating that callable
`backend=` factories are removed in 0.7.0 — and three of the four agent build
paths pass exactly that (`core/engine.py:735` CLI-local, `:995` orchestrator,
`:1224` tinker; only Docker passes an instance). Upgrading to deepagents 0.7
would stop every non-Docker agent in the system from building. This is `F-T05`
(no version ceilings) materializing as a dated, confirmed break rather than a
hypothetical one, and it is the strongest single justification for the pins added
in Phase 0.

**Row 7 remains the open gap.** The LangGraph interrupt/resume protocol
(`F-T06`) is hand-implemented twice in `core/streaming.py` (`F-D03`) and has no
tripwire. It fails loudly but late — mid-turn, in front of a user. Covering it
needs the driver-loop consolidation in Phase 4, so it is deliberately still open.
