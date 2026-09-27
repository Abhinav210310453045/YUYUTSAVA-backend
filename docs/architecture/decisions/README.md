# Architecture decisions

The structural decisions that shaped today's code. Each record states the
context, the decision, the alternatives that were rejected and why, and the
consequences, including the negative ones.

They were written as proposals by the structural review of 2026-08-08, which
was retired at the close of [Code Review v0.1.0](../../code-review/v0.1.0/README.md).
Their bodies are kept as written; each gained a dated status line. What became
of every finding they address is in the
[legacy ledger](../../code-review/v0.1.0/10-legacy-ledger.md).

| ADR | Decision | Status |
|---|---|---|
| [001](ADR-001-agent-build-pipeline.md) | Agent builders driven by capability profiles as data | Accepted; implemented incrementally rather than as a parallel pipeline |
| [002](ADR-002-storage-mapper-layer.md) | Store twins collapsed behind a dialect adapter | Accepted; 18 of 19 domains; schema centralization and `usage` carried |
| [003](ADR-003-composition-root-modules.md) | `ports/` and a composition root split into subsystem builders | Accepted and implemented |
| [004](ADR-004-framework-boundary.md) | A boundary between YUYUTSAVA policy and the agent framework | Accepted 2026-08-10 and executed; the constructing half of `F-T03` remains by design |

ADR-001 and ADR-002 were independent; ADR-003 built on both, and ADR-004 on
ADR-001 and ADR-003.

A new decision gets the next number, the same sections, and a status line that
is kept current.
