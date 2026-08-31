# ADR-001 — Replace the three agent builders with a declarative spec + pipeline

**Status:** Proposed
**Addresses:** [`F-D01`](../03-findings-dry-kiss.md#f-d01), [`F-S02`](../02-findings-solid.md#f-s02), [`F-K02`](../03-findings-dry-kiss.md#f-k02), [`F-D06`](../03-findings-dry-kiss.md#f-d06), partially [`F-T03`](../04-findings-thirdparty-coupling.md#f-t03)
**Phase:** 1

---

## Context

`core/engine.py` contains three master-agent builders — `build_cli_deepagent`
(321 lines, 32 params), `build_orchestrator` (242 lines), `build_tinker_agent`
(246 lines, 30 params). They perform the same nine assembly steps in the same
order and differ in a handful of policy choices.

Those choices are not recorded anywhere. They are the *difference between three
functions*, which means:

- six capabilities already diverge silently across the three
  ([`F-S02`](../02-findings-solid.md#f-s02) has the matrix);
- a fourth master agent costs ~250 copied lines and inherits whichever subset of
  the matrix its source happened to have;
- 27 optional parameters encode combinations whose validity is checked, if at
  all, by `ValueError`s scattered through the body.

## Decision

**Make agent composition declarative. An agent becomes a value; building one
becomes a function of that value.**

```python
@dataclass(frozen=True)
class AgentSpec:
    role: str
    prompt: PromptRenderer
    workspace: WorkspacePolicy
    tools: frozenset[ToolFamily]
    policies: tuple[PolicyRef, ...]      # was: the middleware list
    injectors: tuple[InjectorRef, ...]   # was: the triplicated block
    subagents: SubagentPolicy
    execution: ExecutionMode = ExecutionMode.LOCAL
    permission: Permission = Permission.OPTIONAL

    def __post_init__(self) -> None:
        # Illegal combinations fail here — at the definition site, by name —
        # instead of 200 lines into a build.
        validate(self)
```

```python
PIPELINE = [
    resolve_model, resolve_checkpointer, resolve_backend,
    build_policy_stack, build_injector_stack,
    build_tool_registry, build_subagent_specs,
    render_system_prompt, compile_graph,
]

def build_agent(spec: AgentSpec, deps: BuildDeps) -> AgentBundle:
    ctx = BuildContext(spec, deps)
    for step in PIPELINE:
        step(ctx)
    return ctx.bundle
```

Existing profiles become data:

```python
CLI_SPEC = AgentSpec(
    role="cli",
    policies=(TOOL_FILTER, FILESYSTEM_PROMPT, VOICE_STYLE, SUBAGENT_GATE, PERMISSION),
    injectors=(MEMORY, SKILLS, CONVERSATION, PREFS),
    execution=ExecutionMode.LOCAL | ExecutionMode.DOCKER,
    ...
)
```

### What this buys, precisely

**The capability matrix becomes reviewable.** Today, "does tinker enforce the
subagent gate?" requires diffing two 250-line functions. After, it is one line in
a tuple, visible in a pull request.

**Middleware ordering becomes enforced rather than commented.** The ordering rule
— `tool filter → offload → compaction → budget → usage` — is currently a comment
repeated three times (`engine.py:543`, `:936`, `:1088`). It becomes an assertion
inside `build_policy_stack`, verified once.

**Invalid configurations fail early and by name.** `async_subagents` without
`async_host_url` currently raises 200 lines into a build, from three separate
copies of the same guard. It becomes a `__post_init__` error at the definition
site.

## Alternatives considered

### A. Builder-pattern class

```python
AgentBuilder().with_memory(store).with_skills(...).build()
```

**Rejected.** It replaces 32 parameters with 32 methods — the same combinatorial
surface, now with mutable intermediate state and no single point at which the
configuration can be validated. It also makes the configuration unprintable and
undiffable, losing the main benefit.

### B. Inheritance — `BaseMasterAgent` with overridable hooks

**Rejected.** The variation between profiles is *combinatorial*, not
hierarchical: tinker wants the CLI's filesystem-prompt override *and* the
orchestrator's budget middleware. Any class hierarchy forces an arbitrary
primary axis and produces exactly the diamond problems inheritance is bad at.
`BaseSubAgent` works well because subagent variation genuinely is hierarchical —
master variation is not.

### C. Extract shared helpers, keep three functions

**Rejected as insufficient.** This is the *natural* next step and it does not
solve the problem. Helpers reduce line count while leaving the capability matrix
implicit — the three functions still each decide which helpers to call, so drift
continues exactly as before. It treats the symptom (`F-D01`) and not the cause
(`F-S02`).

Worth noting: `_context_middleware` (`engine.py:330`) and `_sync_subagent_specs`
(`:387`) already *are* this approach. They have not prevented the divergence.

### D. Do nothing

**Rejected.** The matrix already has six divergent columns across three builders.
A fourth master agent is a foreseeable requirement, and the cost is 475 lines
plus an undocumented capability decision.

## Consequences

### Positive

- Scenario A (new master agent): ~475 lines → ~15. See
  [05](../05-change-cost-scenarios.md#scenario-a--add-a-new-master-agent).
- The capability matrix becomes data: testable, diffable, reviewable.
- `core/engine.py` drops from 1319 lines to an estimated < 400.
- Specs are printable — an operator can log exactly how a running agent was
  configured, which is impossible today.
- `compile_graph` becomes the *single* framework call site, a prerequisite for
  [ADR-004](ADR-004-framework-boundary.md).

### Negative

- One indirection added: reading `AgentSpec` no longer shows the assembly. This
  is the standard declarative trade — mitigated by keeping `PIPELINE` a literal,
  readable list of nine named functions rather than a plugin registry.
- Genuinely one-off behavior (Docker execution mode, currently CLI-only) must
  either become a spec field or an explicit escape hatch. **Prefer a spec field**;
  escape hatches reintroduce the problem.
- ~2 weeks of work on the hot path with no user-visible benefit.

### Risk and mitigation

| Risk | Mitigation |
|------|-----------|
| Behavior change during refactor | Step 1.4 fingerprint equivalence gate — assert identical middleware/tools/subagents/prompt-hash for all profiles before deleting anything |
| Big-bang merge | Old signatures survive as thin adapters (Step 1.3); every existing call site keeps working while the pipeline carries all traffic |
| Over-abstracting for hypothetical agents | Encode only the three profiles that exist. Add fields when a fourth agent needs one, never speculatively |
| Discovering the matrix divergences are bugs | Expected. Fix them as separate reviewable commits, not silently inside the refactor |

## Verification

- Fingerprint equivalence for all three profiles × their flag combinations
- A test asserting each profile's declared capability set
- Add a fourth profile as the acceptance test: it must cost < 30 lines and
  require no edit to `core/engine.py`
