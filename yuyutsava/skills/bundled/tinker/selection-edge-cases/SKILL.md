---
name: selection-edge-cases
description: |
  Handling selection-context blocks — the objective/note references the
  user checkbox-selected on the board before a message. Use when a turn
  opens with <selection-context>, especially large, stale, or mixed
  selections.
---

## Pattern

1. Re-read every referenced id with todo_get before acting — the excerpt
   in the block is a preview, not the current content.
2. **Missing ids** (deleted since selection, e.g. by a concurrent edit):
   name exactly which references no longer exist, then continue with the
   rest. Never guess at what a dead id contained.
3. **Large selections (>10 items)**: don't process blindly — reflect the
   scope back in one line ("you've selected 14 notes across 3 objectives —
   comparing them for contradictions, yes?") and let the user confirm or
   narrow before deep work.
4. **Mixed objective + note selections**: group the notes under their
   objectives and treat un-grouped notes as context for the whole set.
   Answer at the objective level unless asked about a specific note.
5. The block scopes THIS turn only. A follow-up message without a block
   returns to whole-card scope — don't keep an old selection alive.

## Gotchas

- The wrapper is UI metadata: never quote `<selection-context>` back, and
  never treat its text as something the user typed.
- Selected items may belong to different phases — respect each one's
  phase when updating (a blocked objective needs its reason, etc.).
