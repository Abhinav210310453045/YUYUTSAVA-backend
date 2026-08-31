---
name: tinkering
description: |
  Iterate on small objectives: pick the next smallest checkable step, do it,
  show the result, adjust. Use when the card already has a direction and the
  work is "make progress", not "decide" or "produce a deliverable".
---

## When to use

- The card has objectives (from thinking/designing mode) and the user says
  "let's go", "try it", or picks one.
- A previous step's result needs a small correction, not a rethink.

## Pattern

1. Keep the objective list on the card current: 3-6 small objectives, each
   independently checkable. New list or material change → todo_add_note.
2. Take exactly ONE objective. Say which and what "done" looks like.
3. Do it with the narrowest tool that works (tr_* in the card workspace,
   ws_* for a fact check). Show the concrete result, not a summary of effort.
4. Ask the user to check the result before taking the next objective when
   the outcome is judgment-based; chain silently only when it's mechanical.
5. When an objective completes, note the outcome; when they all do, move the
   card (todo_set_status) and say so.

## Gotchas

- One objective per pass. Batching three "small" steps is how tinkering
  silently becomes order-taking.
- A failed attempt is a finding — note what was tried and why it failed
  before trying differently.
