---
name: bg-completion-artifacts
description: |
  Handling a background subagent's completion — parsing the summary,
  surfacing produced artifacts, reacting to failures. Use when the
  in-flight tasks block shows a finished background task or a
  subagent_completed wake arrives.
---

## Pattern

1. Read the completion summary once. A trailing `ARTIFACTS: <id>, <id>`
   line means the subagent produced showable artifacts — call
   artifact_show(<id>) for EACH id so they render inside your reply.
   Summaries without the trailer have nothing to embed; don't hunt.
2. Synthesize, don't forward: fold the summary into your own reply in the
   user's terms. Strip internal ids except artifact embeds.
3. **Failure** (status error/timeout/cancelled) → report what failed in one
   sentence, with the error's gist. Offer ONE relaunch with a sharper
   description; never auto-relaunch repeatedly, never silently drop a
   failed task.
4. Terminal status is FINAL — act on it once; never re-check the same
   task_id afterwards.

## Gotchas

- An empty or "I'm still researching…" summary from a *completed* task is
  a failure in disguise — treat it as one (report + offer relaunch).
- Multiple tasks finishing at once: acknowledge each in one line, expand
  only the one the user cares about.
