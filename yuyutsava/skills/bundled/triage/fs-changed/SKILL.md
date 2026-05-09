---
name: fs-changed
description: |
  Classification hints for fs.changed events. Use when the event topic
  is fs.changed to decide between drop, log, and propose — and to pick
  the right subagent and instruction.
---

## Classification rules

| Condition | Action | Subagent |
|---|---|---|
| kind=created, ext=pdf, path in ~/Downloads | propose | file-organizer |
| kind=created, ext in (zip,tar,gz), path in ~/Downloads | propose | file-organizer |
| kind=created, ext in (jpg,png,mp4,mov), path in ~/Downloads | propose | file-organizer |
| kind=created, path NOT in watched roots | drop | — |
| kind=modified, burst_count > 50 | drop (build noise) | — |
| kind=modified, ext in (py,js,ts,md), burst_count < 5 | log | — |
| kind=deleted | drop (usually) | — |

## Bias

- Drop by default. Propose only when there is an unambiguous, reversible
  action a subagent can take.
- Never propose for system files (.DS_Store, Thumbs.db, *.tmp).
- Urgency 2 for large files (>10 MB), urgency 1 otherwise.
