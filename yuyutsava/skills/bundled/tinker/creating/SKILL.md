---
name: creating
description: |
  Produce a durable artifact for the card: a document, chart, table, code
  file, or rendered visual. Use when the user asks for a concrete deliverable
  (not a discussion) and the shape of it is already agreed.
requires_tools:
  - vis_chart
---

## When to use

- "Write it up", "make the chart", "draft the doc", "generate the file".
- A tinkering objective's output deserves to outlive the chat.

## Pattern

1. Confirm the artifact's shape in one line (format, audience, length) —
   tr_ask_user only if genuinely ambiguous.
2. Produce it in the card workspace: files via tr_write_file under the card's
   workspace dir; charts/diagrams/tables via the vis_* tools (load with
   tool_search first).
3. Attach it: todo_attach_artifact with the right kind (file / image /
   diagram / artifact) and a human title.
4. Add one note saying what the artifact is and what decision/insight it
   captures — an attachment without a note is a mystery file.

## Gotchas

- Never write deliverables outside the card workspace (EXTERNAL zone will
  prompt, and the file would be orphaned from the card).
- Re-generating an existing artifact? Attach the new version with a version
  hint in the title instead of silently replacing context.
