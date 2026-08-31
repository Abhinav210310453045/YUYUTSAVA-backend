# YUYUTSAVA Documentation

Start with [architecture/overview.md](architecture/overview.md) — it is the
authoritative, code-grounded reference and covers both operating modes.

## Architecture

How the system is built and why.

| Document | What it answers |
|---|---|
| [architecture/overview.md](architecture/overview.md) | The whole system: both operating modes, every major subsystem, and the data/control flows between them. Start here. |
| [architecture/daemon.md](architecture/daemon.md) | Diagram-first companion covering the daemon's runtime flows — boot, event ingestion, triage, orchestration, shutdown. |
| [architecture/transport.md](architecture/transport.md) | Wire level: how input physically reaches the LLM and how output gets back. Frame catalogs, `seq`/replay semantics, the voice PCM path. |
| [architecture/task-runner.md](architecture/task-runner.md) | The filesystem/shell gateway — zones, permission model, rule table. |
| [mcp_architecture.html](mcp_architecture.html) | MCP connectivity: client manager lifecycle, tool scoping, and what is *not* supported (SSE only, no sampling, no elicitation). Hand-drawn SVG diagrams — GitHub shows this as source, so open it in a browser. |

### Architecture review

[architecture/review/](architecture/review/) is a strict evaluation of the
`yuyutsava/` package against SOLID, DRY and KISS, plus an analysis of coupling
to third-party framework abstractions. Findings are keyed (`F-S07`, `F-D04`,
`F-T01`…) and referenced directly from source docstrings.

Read [review/00-executive-summary.md](architecture/review/00-executive-summary.md)
first, or jump to [review/06-remediation-plan.md](architecture/review/06-remediation-plan.md)
for the plan. The four accepted decisions live in
[review/adr/](architecture/review/adr/).

## Reference

Contracts and APIs — the things you look up rather than read through.

| Document | What it covers |
|---|---|
| [reference/api-v1.md](reference/api-v1.md) | The `/v1` daemon HTTP contract. Source of truth for clients. |
| [reference/visual-tools.md](reference/visual-tools.md) | The `yuyutsava/visuals` rendering library — charts, tables, math, code images, diagrams. |

## Guides

Task-oriented walkthroughs.

| Document | What it covers |
|---|---|
| [guides/voice.md](guides/voice.md) | The voice interface: what was built, and an end-to-end test checklist. |
| [guides/async-subagents.md](guides/async-subagents.md) | Runbook for exercising every background-subagent code path (~20 min). |
| [guides/windows.md](guides/windows.md) | OS-invariance layering and the System Warden; what is platform-specific and where. |
| [guides/macos-branding.md](guides/macos-branding.md) | Finishing the macOS dock name and menu bar at build time. |

## Design notes

Proposals and specifications. Some describe shipped systems, others are
unimplemented designs — each states its own status at the top.

| Document | Status |
|---|---|
| [design/todo-board.md](design/todo-board.md) | Shipped. The TODO board + TinkerAgent spec; cited directly from the source. |
| [design/document-hybrid-index.md](design/document-hybrid-index.md) | Design only — not built. |
| [design/search-strategy.md](design/search-strategy.md) | Proposal — not implemented. |
| [design/tool-discovery.md](design/tool-discovery.md) | Proposal — not implemented. |

## Conventions

- Paths in these documents are relative to the repository root. `$REPO` stands
  for wherever you cloned it.
- Line-number references into source (`engine.py:1148`) drift as the code moves.
  Treat the symbol name as authoritative and the line number as a hint.
