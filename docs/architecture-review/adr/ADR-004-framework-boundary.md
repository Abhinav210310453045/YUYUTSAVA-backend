# ADR-004 — Draw a boundary between YUYUTSAVA policy and the agent framework

**Status:** Proposed — **requires an explicit go/no-go decision**
**Addresses:** [`F-T01`](../04-findings-thirdparty-coupling.md#f-t01), [`F-T02`](../04-findings-thirdparty-coupling.md#f-t02), [`F-T03`](../04-findings-thirdparty-coupling.md#f-t03), [`F-T06`](../04-findings-thirdparty-coupling.md#f-t06), [`F-D03`](../03-findings-dry-kiss.md#f-d03)
**Phase:** 4 (optional) · **Requires:** ADR-001, ADR-003

---

## Context

64 of 350 modules (18.3%) import LangChain, LangGraph, or deepagents directly.
More significantly, **every cross-cutting concern YUYUTSAVA owns is expressed in
a vocabulary it does not own**:

- 14 policy classes subclass `langchain.agents.middleware.AgentMiddleware`;
- `BaseChatModel` is the currency in 18 modules;
- `create_deep_agent` is the only agent constructor (4 sites);
- `langgraph.types.interrupt()` is called from tool and policy code;
- the resume protocol is hand-implemented twice, in two 226-line functions.

Budget ceilings, cost accounting, consent policy, transcript persistence, and
skill retrieval are *product*. None of them are LangChain concerns. All of them
are LangChain subclasses.

`F-T07` shows this surface is already moving: the codebase straddles two
generations of the middleware hook API (`before_model` vs `wrap_model_call`) and
imports the same symbol via two different paths.

## The decision to make first

**This ADR should not be executed by default.** Phase 0 (version ceilings +
characterization tests, ~2 days) caps the *acute* risk at a fraction of Phase 4's
4 weeks. Phase 4 buys framework *independence*, which is only worth the cost if
at least one of these holds:

1. There is a realistic prospect of changing or supplementing the agent framework.
2. Policy logic must run outside a graph — batch evaluation, replay, offline
   scoring.
3. Policy tests are too slow or fragile today because they require a framework.
4. Framework API churn is causing recurring pain beyond what Phase 0 contains.

**Item 3 is the most likely justification.** The project already avoids heavy
import tests because the framework makes them slow — which means the 14 policy
classes are effectively untested. That is a present cost, not a hypothetical one.

**If none hold, record "we chose not to" as an accepted ADR and stop.** That is a
legitimate outcome and a useful artifact.

## Decision (if approved)

### 1. `yuyutsava/policy/` — our own policy protocol

```python
class Policy(Protocol):
    """A YUYUTSAVA cross-cutting concern, in YUYUTSAVA's own types."""
    def on_model_call(self, ctx: TurnContext) -> PolicyAction: ...
    def on_tool_call(self, ctx: ToolContext) -> PolicyAction: ...
```

One adapter, not fourteen:

```python
class LangChainPolicyAdapter(AgentMiddleware):
    """The single place this system knows about AgentMiddleware."""
    def __init__(self, policies: Sequence[Policy]) -> None: ...
    def wrap_model_call(self, request, handler): ...   # → TurnContext
    def wrap_tool_call(self, request, handler): ...    # → ToolContext
```

`BudgetMiddleware`, `UsageRecorder`, `PermissionMiddleware`, and the other 11
become plain classes testable with a constructed `TurnContext` — no framework
import, no graph, fast tests.

### 2. `ModelHandle` — do this regardless of Phase 4

```python
@dataclass(frozen=True)
class ModelHandle:
    model: BaseChatModel          # still reachable for code that needs it
    name: str
    provider: str
    capabilities: frozenset[Capability]
```

~50 lines. Deletes `model_name_of`'s six-way duck-typing across vendor SDK
classes (`llm/factory.py:22-38`) by having `Provider.build()` return what it
already knows. Gives capability checks a declared home instead of `getattr`
probes.

**This is the cheapest item in the entire review with a real payoff. Ship it in
Phase 1 even if Phase 4 is rejected.**

### 3. `AskUser` port

```python
class AskUser(Protocol):
    async def ask(self, prompt: AskPrompt) -> str: ...
```

Domain code calls `await ctx.ask(...)`; a `LangGraphAskUser` adapter implements it
with `interrupt()`. The consent core (`yuyutsava/consent/`) is already
well-factored and framework-free — this makes its *delivery* match, and makes
consent logic testable without a graph.

### 4. `Agent` protocol + one driver loop

```python
class Agent(Protocol):
    def astream(self, input: AgentInput) -> AsyncIterator[StreamEvent]: ...
    async def ainvoke(self, input: AgentInput) -> AgentResult: ...
```

The deepagents graph becomes the default implementation rather than the
definition. Collapse `astream_agent` / `astream_agent_iter` (`F-D03`) into one
driver plus a sink — the CLI's `cli/render/` package already has the sink shape,
so most of that work is done.

## Alternatives considered

### A. Do nothing beyond Phase 0

**The default, and defensible.** Version ceilings plus three characterization
tests convert the silent failures into loud ones for ~2 days of work. If nothing
in the "decision to make first" list holds, this is the right answer.

Its weakness: it does not address the testability cost (item 3), which is a
present, recurring tax rather than a contingency.

### B. Vendor / fork `deepagents`

**Rejected.** Trades a dependency for a maintenance burden and forfeits upstream
fixes. The coupling in `F-T04` is to behavior we could pin with a fork, but the
cost of tracking upstream permanently exceeds the adapter cost.

### C. Wrap everything — full hexagonal architecture

**Rejected as disproportionate.** Wrapping `BaseChatModel`, `BaseTool`,
`AIMessage`, and `AgentState` in our own types would touch nearly every module
for benefit that is mostly theoretical. `BaseChatModel` in particular is a broad,
stable, well-designed interface every provider implements — the proportionate fix
is `ModelHandle` (item 2), not a wrapper hierarchy.

**Draw the boundary around what is ours (policy, HITL, agent identity), not
around what is theirs (message types, tool types).** That is the distinction this
ADR is built on.

### D. Migrate to raw LangGraph, drop deepagents

**Rejected as currently scoped**, but worth noting: it would remove the `F-T04`
internal-dependency risk entirely, since the three fragile couplings are all to
deepagents specifics — the filesystem prompt block, the name-match override, the
sandbox backend. The cost is reimplementing what `create_deep_agent` provides.

Reconsider if `F-T04` breaks in practice, or if deepagents' release cadence
proves incompatible with this project's needs.

## Consequences

### Positive

- 14 framework subclasses → 1 adapter. A hook-signature change becomes a
  one-file change instead of a 14-file change.
- Policies become plain, fast-to-test objects — directly addressing the reason
  they are effectively untested today.
- A policy *inventory* exists for the first time: today "what policies does
  YUYUTSAVA enforce?" is answerable only by grepping for a third-party base class.
- Consent/HITL becomes testable without a graph.
- Framework upgrade risk drops from "unbounded" to "one adapter file".

### Negative

- **4 weeks, high risk, zero user-visible benefit.** The largest investment in
  this review with the least tangible return.
- Adapters add indirection to the hot path — every model and tool call passes
  through one more layer. Measure before and after; the overhead should be
  negligible, but "should be" is not a measurement.
- Our `Policy` protocol may not express something the framework's richer hooks
  allow. Expect at least one policy to need an escape hatch. **Grant it
  explicitly and document it** rather than weakening the protocol for everyone.
- Risk of building an abstraction that only ever has one implementation — the
  classic failure mode of this kind of work.

### Risk and mitigation

| Risk | Mitigation |
|------|-----------|
| Abstraction never pays off (framework never changes) | Justify on **testability** (item 3), which pays immediately, not on portability, which may never pay |
| Adapter cannot express a policy's needs | Migrate the most demanding policy first — `PermissionMiddleware`, which already carries `# type: ignore[misc]` because the base contract does not fit it. If the adapter handles that one, it handles the rest |
| Performance regression | Benchmark a representative turn before and after; gate the merge on it |
| Scope creep into wrapping message/tool types | Explicitly out of scope per Alternative C. Enforce in review |

## Verification

- All 14 policies implement `Policy`; exactly one `AgentMiddleware` subclass
  remains in the codebase
- Every policy has a unit test that imports no framework module and runs in
  milliseconds
- Turn-latency benchmark within noise of the pre-change baseline
- `grep -rl "langchain\|langgraph\|deepagents" yuyutsava | wc -l` drops from 64
  toward the target of < 25
