---
name: todo-capture-playbook
description: |
  Capturing tasks, ideas, reminders, and plans onto the user's TODO board
  from chat. Use whenever the user says remember this, track this, add a
  todo, plan this later, don't let me forget — or mentions future work
  worth keeping.
---

## Pattern

1. **Find before you create.** Check todo_recall(query) first — semantic
   recall over board notes finds the topic even when titles differ ("the
   website redesign" usually IS the existing "Redesign landing page" card).
   No hit and still unsure → todo_list to fuzzy-match titles. One card per
   topic; never create a near-duplicate.
2. Existing card → add the new information as a note (todo_add_note,
   author defaults to your identity — never "user"). New topic → todo_add
   with a short, specific title (the user's noun phrase, not a sentence).
3. Past board notes are also the record of DECISIONS (naming, approach,
   scope) — the same todo_recall check from step 1 covers them; don't
   re-derive what the user already settled.
4. Confirm the capture in one clause ("added to your board: <title>") —
   don't narrate the tool calls.

## Gotchas

- "Remember X" for a durable *preference* (not a task) → mem_save
  (kind="preference"), not a TODO card.
- Don't dump the whole conversation into a note — one focused note per
  capture.
- You have capture scope only (add/list/get/recall). Objectives, phases,
  and attachments belong to the TinkerAgent on the card itself — tell the
  user to open the card to think it through.
