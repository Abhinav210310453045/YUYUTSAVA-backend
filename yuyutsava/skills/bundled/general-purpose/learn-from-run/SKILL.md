---
name: learn-from-run
description: |
  The reflection rubric for saving skills and preferences after a task —
  what qualifies as a reusable pattern, how to scope it global vs own,
  how to refine an existing skill. Use at the end of a run when deciding
  whether to sk_write_skill or mem_save.
---

## What qualifies as a skill

Save a pattern only if ALL hold:
- It combined tools or steps in a non-obvious sequence (the next agent
  wouldn't guess it).
- It would transfer to *similar future tasks* — not tied to one file,
  one workspace, or today's data.
- It isn't already captured (sk_search_skill first; a near-match means
  REFINE, not duplicate).

## Scoping

- `scope="global"` — any agent could benefit (a web-research recipe, a
  file-format trick, an API's quirk).
- `scope="own"` — specific to your kind of task (how *you* structure a
  research summary; a board workflow only a tinker runs).
When unsure, prefer "own" — a master can still find it; global namespace
stays clean.

## Writing discipline

- Body ≤ 150 words: what was done, which tools, the one gotcha.
- Description = trigger keywords (what future task would need this), not
  a summary of what you did today.
- To refine an existing skill, reuse its exact name — the write replaces
  it. Fold the new lesson in; don't append forever.

## Preferences (not skills)

A durable USER preference ("prefers CSV over XLSX", "always UK dates") →
mem_save(text, kind="preference"). One sentence, present tense, no
task-specific context.
