---
name: board-hygiene
description: |
  Keeping a TODO card's board healthy — empty cards, concurrent edits,
  note authorship, phase discipline, journey preconditions. Use when a
  card looks empty/stale, writes conflict, or before generating the
  journey document.
---

## Pattern

1. **Empty card** (no objectives, no notes): don't fabricate structure.
   Ask one sharpening question, then OFFER a 3-6 objective decomposition
   and create it only on agreement.
2. **Concurrent edits**: the user edits the same board you do. Call
   todo_get immediately before a batch of writes — never operate on a
   card snapshot from several turns ago. If something you expected is
   gone, say so and adapt; don't recreate deleted items.
3. **Authorship is identity**: notes you write carry author="tinker" —
   never write a note pretending to be the user, and never edit the
   *meaning* of a user's note (append your take as your own note instead).
4. **Phase discipline**: blocked/abandoned without a `reason` and
   completed without an `outcome` are incomplete moves — fill the field
   in the same todo_update_objective call.
5. **Journey preconditions**: before todo_generate_artifact(block=
   "journey"), write exactly ONE fresh `## Reflection` note. If phases
   moved without reasons/outcomes, backfill them first — the document
   compiles what's recorded, not what you remember.

## Gotchas

- One focused note per insight; never dump a transcript.
- Objectives are rows, not prose — a "plan" note that's really a list
  should become todo_add_objective rows (with the user's OK).
