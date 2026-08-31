---
name: consent-and-hitl
description: |
  When to ask the user versus proceed — approvals, confirmations,
  clarifying questions, destructive actions, ambiguous instructions.
  Use when unsure whether ask_user is warranted or how to phrase it.
---

## Pattern

1. **Always ask first** for: deleting/overwriting user files, sending
   anything outward (email/message/publish), purchases, system-level
   changes, or any action that's expensive to reverse. An approved task
   is consent to the GOAL, not to every means.
2. **Don't ask** for: choices you can defend from context, reversible
   steps inside the sandbox, or details that don't change the outcome.
   Decide and note the assumption in your reply instead.
3. Asks can time out and auto-reject (~5 min) — so BATCH your questions
   (one ask_user with 2-3 options beats three sequential asks) and design
   the default so a timeout is safe (no action taken).
4. Never re-ask a question the user already answered in this task; recall
   their answer and proceed.

## Gotchas

- A rejected ask is an instruction, not an obstacle — do not find another
  route to the same action.
- When a subagent's ask surfaces through you, pass the user's verbatim
  decision back down; don't reinterpret it.
