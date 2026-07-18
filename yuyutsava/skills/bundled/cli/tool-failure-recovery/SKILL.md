---
name: tool-failure-recovery
description: |
  Recovering from denied, blocked, or failed tool calls — permission
  denials, zone blocks, error statuses, timeouts, missing capabilities.
  Use when a tr_*/ws_*/vis_* result comes back status=denied or
  status=error, or a command keeps failing.
---

## Pattern

1. **error** → read `error` + `hint`. Fix the call ONCE (bad path, missing
   param, wrong zone) and retry. If the retry fails too, stop: report the
   error text verbatim plus the hint, and what you'd need to proceed.
2. **denied** → the user or a zone rule blocked it. Read `alternatives` and
   either take one (usually: write into SANDBOX/OUTPUT instead of the
   EXTERNAL path) or ask the user which they prefer. Never re-attempt the
   identical denied call — a denial is an answer.
3. **Timeout / hang** → report how far it got; offer to rerun in the
   background (start_async_task) rather than blocking the chat again.
4. Multi-step task with one failed step → finish the independent steps,
   then report exactly which step failed and why — never silently skip.

## Gotchas

- NEVER fabricate success or paper over a failure with "done!". The user
  trusts your report more than the result.
- A denial on an EXTERNAL write usually means the deliverable belongs in
  the OUTPUT dir — offer that path explicitly.
- Repeated 4xx from a web tool → the query/site is the problem; switch
  tools (ws_tavily ↔ ws_exa ↔ tr_fetch_url) instead of retrying harder.
