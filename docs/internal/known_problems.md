# Known Problems

Running log of bugs and design issues discovered in the YUYUTSAVA agent stack.
Newest entries at the top. Each entry records the symptom, the root cause(s),
where the bug lives, and the proposed fix so the issue can be picked up later
without re-deriving context.

---

## P-001 — Multi-topic CLI task halts silently after one subagent call

**Date observed:** 2026-05-25
**Branch:** `yuyutsava-daemon`
**Session id:** `cli-1779546005-5e56dcc5-88b7-4a1e-9407-b207dfa05607`
**Severity:** High — failure is silent and looks like success in logs.

### Symptom

CLI invocation:

```
yuyutsava --verbose "<topic 1> ... <topic 2> ... <topic 3> ... <topic 4> ...
<topic 5> ..."
```

The parent agent:
1. Wrote a 5-item todo list (one per topic).
2. Spawned a single `task(subagent_type="general-purpose")` for topic #1.
3. The subagent did two tool calls (`tr_execute` curl, then `tr_write_file`
   to `/sandbox/extract_info.py`) and stopped.
4. The `task` tool returned an empty `ToolMessage` with `status: success`.
5. The parent emitted one chatty placeholder line
   (*"I'm researching the latest news in India related to temperature..."*)
   and exited the react loop without calling any further tools.

The four remaining todos were never picked up. No error was surfaced to the
user — the run looked clean.

### Root causes (four bugs stacked)

#### Bug 1 — `general-purpose` subagent has no workspace/sandbox context

The CLI parent's system prompt
([`yuyutsava/core/prompts.py:110-127`](../yuyutsava/core/prompts.py#L110-L127))
bakes in real paths for workspace, sandbox, and output dir. When it delegates
via deepagents' `task` tool, the spawned subagent uses **its own** prompt at
[`yuyutsava/agents/general_purpose/agent.py:18-64`](../yuyutsava/agents/general_purpose/agent.py#L18-L64),
which mentions zero paths. The subagent guessed `/sandbox/extract_info.py`.
On macOS `/sandbox` does not exist and `/` is read-only under SIP, so
[`executor.py:138`](../yuyutsava/agents/task_runner/executor.py#L138)
(`path.parent.mkdir(...)`) failed with `[Errno 30] Read-only file system`.

#### Bug 2 — Subagent stops after one failure with no final text

After the `tr_write_file` error, the subagent (`gemini-2.5-flash`) produced a
message with no text content and ended its react loop. It did not retry under
the correct sandbox, did not summarize the curl output, did not report
failure. Its system prompt has a "branch on status" contract
([general_purpose/agent.py:42-49](../yuyutsava/agents/general_purpose/agent.py#L42-L49))
but no rule mandating a non-empty final message or a fallback path
(e.g. `tr_ask_user` for the real sandbox, or write to `/tmp`).

#### Bug 3 — Empty subagent reply becomes a `status: success` empty ToolMessage

`deepagents.middleware.subagents._return_command_with_state_update` does:

```python
message_text = result["messages"][-1].text.rstrip() if result["messages"][-1].text else ""
```

(`.venv/lib/python3.11/site-packages/deepagents/middleware/subagents.py:414`)

If the subagent's last message has no text, the parent gets a *successful*,
empty `ToolMessage` with no error signal. There is no "subagent produced no
answer" sentinel for the parent to react to.

#### Bug 4 — Parent treats empty `task` result as done

Given an empty tool result, the parent (also `gemini-2.5-flash`) emitted one
sentence and ran `finish_reason=stop` without spawning the other four research
subagents in parallel (which the deepagents `TASK_TOOL_DESCRIPTION` explicitly
encourages) and without falling back to `ws_tavily_search` / `ws_exa_search`
(both registered, per the boot log).

### Proposed fixes (in priority order)

1. **Inject real paths into every subagent prompt at build time.**
   Wrap the spec returned by
   [`base_sub_agent.py:215-233`](../yuyutsava/agents/base_sub_agent.py#L215-L233)
   so it appends a `WORKSPACE CONTEXT` block built from
   `self._task_runner.workspace_root` and `.sandbox_root`. Alone, this
   prevents the `/sandbox` hallucination.

2. **Guard `tr_write_file` against hallucinated absolute paths.**
   Extend `_resolve_path` in
   [`yuyutsava/agents/task_runner/tools.py:31-44`](../yuyutsava/agents/task_runner/tools.py#L31-L44)
   to detect paths outside workspace/sandbox/output and return a structured
   error like
   `"path '<x>' is outside sandbox at <real>; did you mean '<real>/extract_info.py'?"`
   instead of letting the `OSError` bubble.

3. **Force non-empty subagent final messages.**
   Add to the general-purpose prompt: *"Your final message MUST be plain
   text — never tool calls. If you cannot complete the task, return a
   one-line failure summary explaining what blocked you."*

4. **Wrap deepagents `task` tool to surface empty results.**
   In a thin post-processing middleware, replace empty `ToolMessage` content
   from `task` with a sentinel like
   `"<subagent returned no content; treat as failure and either retry or report to user>"`.
   Fixes bugs 3 and 4 together.

5. **Model / strategy.** For multi-topic research, `gemini-2.5-flash` halts
   too easily. Either route the parent to a stronger model, or strengthen the
   parent prompt to prefer parallel `ws_*` web-search calls over single-topic
   subagent delegation.

### Acceptance criteria

- Re-running the failing prompt completes all five topics (or fails loudly
  with a user-visible reason).
- Subagents never invent absolute paths outside the configured zones.
- A subagent that ends with no text produces a clear error in the parent's
  context, not a silent empty success.

### References

- TaskRunner executor: [`yuyutsava/agents/task_runner/executor.py`](../yuyutsava/agents/task_runner/executor.py)
- TaskRunner tools: [`yuyutsava/agents/task_runner/tools.py`](../yuyutsava/agents/task_runner/tools.py)
- Subagent base: [`yuyutsava/agents/base_sub_agent.py`](../yuyutsava/agents/base_sub_agent.py)
- General-purpose prompt: [`yuyutsava/agents/general_purpose/agent.py`](../yuyutsava/agents/general_purpose/agent.py)
- CLI prompts: [`yuyutsava/core/prompts.py`](../yuyutsava/core/prompts.py)
- deepagents `task` tool: `.venv/lib/python3.11/site-packages/deepagents/middleware/subagents.py:374-471`
