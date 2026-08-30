# Architecture Decision Records

Proposed structural decisions arising from the
[architecture review](../README.md). Each ADR states the context, the decision,
the alternatives that were rejected **and why**, and the consequences — including
the negative ones.

**None of these are accepted.** They are proposals for a decision, written so
that the decision can be made from the document rather than from a re-reading of
the codebase.

| ADR | Decision | Addresses | Phase | Status |
|-----|----------|-----------|-------|--------|
| [001](ADR-001-agent-build-pipeline.md) | Replace the three agent builders with a declarative `AgentSpec` + pipeline | `F-D01` `F-S02` `F-K02` `F-D06` | 1 | Proposed |
| [002](ADR-002-storage-mapper-layer.md) | Collapse the store twins behind a dialect adapter and a domain registry | `F-D02` `F-D04` `F-S04` `F-S07` `F-S10` `F-S12` | 2 | Proposed |
| [003](ADR-003-composition-root-modules.md) | Extract `ports/` and split the composition root into subsystem builders | `F-S01` `F-S05` `F-S08` `F-K03` | 3 | Proposed |
| [004](ADR-004-framework-boundary.md) | Draw a boundary between YUYUTSAVA policy and the agent framework | `F-T01` `F-T02` `F-T03` `F-T06` | 4 | **Proposed — needs go/no-go** |

## Dependencies between them

```
ADR-001 ──┐
          ├──> ADR-003 ──> ADR-004
ADR-002 ──┘
```

ADR-001 and ADR-002 are independent of each other and can be worked in parallel
— they touch disjoint modules (`core/engine.py` vs `*/store.py`) and meet only in
`daemon/bootstrap.py`, which ADR-003 restructures afterward.

ADR-003 requires both, because `build_daemon` is long *because of* the branching
those two remove. Splitting it first means splitting it twice.

ADR-004 requires ADR-001 (a single graph-construction site) and ADR-003
(`ports/`, which gives the policy protocols a dependency-free home).

## A note on ADR-004

ADR-004 is the only one requiring a strategic decision rather than a scheduling
one. The others reduce cost-of-change and are justified on that basis alone.
ADR-004 buys framework independence, which is worth its 4 weeks only under
conditions listed in the document.

**Deciding not to do it is a valid outcome and should be recorded as an accepted
ADR** — a written "we considered framework independence and declined it, for
these reasons, and will revisit if X" is more useful than silence.

Two items inside ADR-004 should ship regardless of that decision, because they
are cheap and pay off immediately:

- **`ModelHandle`** (~50 lines) — deletes the six-way duck-typing in
  `model_name_of` and gives model capabilities a declared home.
- **Phase 0's characterization tests** — already scheduled independently in the
  [remediation plan](../06-remediation-plan.md#phase-0--freeze-the-bleeding).
