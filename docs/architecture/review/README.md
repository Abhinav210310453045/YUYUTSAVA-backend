# YUYUTSAVA Architecture Review — `yuyutsava/`

A strict evaluation of the `yuyutsava/` package against **SOLID**, **DRY**, and **KISS**,
with **extensibility and modularity treated as the P0 criterion**, plus a second
analysis of **coupling to third-party framework abstractions**.

> **Evaluation stance.** Findings are derived from what the code *does* — its
> import graph, class topology, branch structure, and the mechanical cost of
> changing it. Docstrings and comments were treated as claims to be verified,
> not as evidence. Where a comment asserts a property the wiring contradicts,
> that gap is itself recorded as a finding (see `F-S07`, `F-D04`).

---

## How to read these documents

Read in order if you are new to the review. Jump straight to `06` if you only
want the plan.

| # | Document | What it answers |
|---|---|---|
| [00](00-executive-summary.md) | Executive summary | What is the verdict, what are the top risks, what do we do first? |
| [01](01-evidence-and-metrics.md) | Evidence & metrics | What was measured, how, and can I reproduce it? |
| [02](02-findings-solid.md) | SOLID findings | Where does each SOLID principle break, with proof? |
| [03](03-findings-dry-kiss.md) | DRY & KISS findings | Where is knowledge duplicated, where is complexity unearned? |
| [04](04-findings-thirdparty-coupling.md) | Third-party coupling | How deeply are we married to deepagents/LangChain/LangGraph, and what breaks on upgrade? |
| [05](05-change-cost-scenarios.md) | Change-cost scenarios | The P0 lens: what does it *actually* cost to extend this system? |
| [06](06-remediation-plan.md) | Remediation plan | The sequenced plan, with the creation flow for each fix. |
| [adr/](adr/) | Decision records | The specific structural decisions being proposed, with alternatives. |

---

## Finding ID scheme

Every finding has a stable ID. **Findings are stated once, in their home
document, and referenced by ID everywhere else.** No finding is restated in
full in two places.

| Prefix | Meaning | Home document |
|--------|---------|---------------|
| `F-S##` | SOLID violation | [02-findings-solid.md](02-findings-solid.md) |
| `F-D##` | DRY violation | [03-findings-dry-kiss.md](03-findings-dry-kiss.md) |
| `F-K##` | KISS violation | [03-findings-dry-kiss.md](03-findings-dry-kiss.md) |
| `F-T##` | Third-party coupling | [04-findings-thirdparty-coupling.md](04-findings-thirdparty-coupling.md) |

Each finding carries:

- **Claim** — the defect, in one sentence.
- **Evidence** — file:line references and a reproducible measurement.
- **Consequence** — what specifically goes wrong, expressed as a scenario.
- **Severity** — `P0`/`P1`/`P2`, scored against extensibility & modularity.

## Severity scale

Severity is scored **against the P0 criterion (extensibility & modularity)**,
not against runtime correctness. A finding that never causes a bug but triples
the cost of every future feature is `P0` here.

| Severity | Meaning |
|----------|---------|
| **P0** | Adding a normal feature requires editing unrelated modules, or the structure actively resists a planned direction. Compounding cost. |
| **P1** | Extension is possible but the pattern must be copied by hand, so drift and divergence are near-certain over time. |
| **P2** | Local clarity or hygiene issue. Real, but bounded and non-compounding. |

---

## Scope and non-scope

**In scope:** the `yuyutsava/` Python package — 350 modules, ~55.6k lines
(excluding `__pycache__`), as of branch `yuyutsava-daemon` @ `f3fa86d`.

**Out of scope:** `electron-app/`, `test/`, `scripts/`, `content/`, and the
existing `docs/` planning documents. Test coverage is referenced only as
evidence of testability (a consequence of DIP), never reviewed on its own merits.

**Not assessed:** runtime correctness, performance, security. This review is
structural. Nothing here should be read as "the system does not work" — it
demonstrably does. The question asked was whether the *structure* will keep
letting it grow, and that is the only question answered.
