---
name: designing
description: |
  Shape the structure of a solution once the idea is sharp: options,
  trade-offs, and a picked direction — often with a diagram on the card.
  Use after thinking-mode has produced a clear problem statement.
requires_tools:
  - vis_diagram
---

## When to use

- The card has a sharpened problem statement and the user asks "how should
  this work / look / be built?".
- Two or more plausible architectures/approaches exist and one must be picked.

## Pattern

1. Lay out 2-3 candidate shapes, each in two sentences: what it is, what it
   costs. No more than three — more is indecision, not rigor.
2. Recommend ONE and say why; confirm the direction with tr_ask_user if the
   choice is expensive to reverse.
3. Draw the picked shape: tool_search('select:vis_diagram') then vis_diagram
   (mermaid/graphviz). Attach the rendered file to the card with
   todo_attach_artifact(kind="diagram", path=...).
4. Record the decision + the discarded options (and WHY discarded) as one
   note — future-you needs the why more than the what.

## Gotchas

- A design note without the rejected alternatives is half a note.
- Diagrams are rendered vis_* PNGs on the card, never mermaid code blocks in
  chat.
