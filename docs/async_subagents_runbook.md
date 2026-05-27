# Async (Background) Subagents — Runbook

Walks through running every code path added by the async-subagents work so you
can validate the system end-to-end on your machine.

Time budget: **~20 minutes** for the full sweep.

---

## 0. Prerequisites

```bash
cd $REPO
uv sync                      # installs deepagents>=0.6.3 + langgraph-cli[inmem]
```

Confirm the right versions are installed:

```bash
uv run python -c "import deepagents, langgraph_api, langgraph_sdk; \
  print('deepagents', deepagents.__version__); \
  print('langgraph_api', langgraph_api.__version__); \
  print('langgraph_sdk', langgraph_sdk.__version__)"
```

Expected: `deepagents 0.6.3`, `langgraph_api 0.8.7`, `langgraph_sdk 0.3.x`.

Async subagents are **off by default**. Enable them with:

```bash
export YUYUTSAVA_ASYNC_SUBAGENTS=1
```

Add it to your `.env` if you want it sticky.

---

## 1. Run the test suites (offline, no LLM keys needed)

These exercise everything we built without touching the LLM. Run them first —
if these fail, the more interactive flows below will too.

### 1a. Unit tests (20 tests, ~0.01s)

```bash
uv run python test/async_subagents/test_unit.py
```

Expected tail:

```
Ran 20 tests in 0.010s
OK
```

Covers: `AsyncTaskMirror` lifecycle, `ChannelRouter` origin-aware routing,
`RemoteAsyncSubagentSpec`, `render_capabilities_block` for sync/local/remote.

### 1b. End-to-end stack (15 checks, ~5–10s)

```bash
uv run python test/async_subagents/e2e_stack.py
```

Spins up the **whole vertical slice** in one process — an in-process
`AsyncSubagentHost`, the daemon's FastAPI on a free loopback port, the
`AsyncTaskHealthWatcher`, a mock graph that calls `interrupt()`, plus an
HTTP client acting as the Electron renderer. Expected tail:

```
== Summary ==
  passed: 15
  failed: 0
```

If you see failures here, capture the full output before continuing — every
later scenario builds on this.

---

## 2. CLI Mode 1 — standalone CLI with a background subagent

`yuyutsava` (no daemon) hosting its own in-process LangGraph server, with
HITL prompts on stdin.

### Setup

```bash
export YUYUTSAVA_ASYNC_SUBAGENTS=1
# plus the usual LLM env (LLM_PROVIDER, API keys, etc.)
```

### Run

```bash
uv run yuyutsava "explain the IC engine in 200 words"
```

### What to look for

While the agent runs, on stderr you should see:

```
CLI Mode 1 async enabled: host=http://127.0.0.1:<port> graphs=['general-purpose']
```

At the end of the task, if any background work was emitted you'd see
inline status banners like:

```
[bg started] general-purpose-bg  task=abc12345  organise downloads…
[bg progress] general-purpose-bg  resumed: 'approve'
[bg done OK] general-purpose-bg  elapsed=3m42s  Moved 17 files
```

If the master delegates to `start_async_task` and the background graph hits
`interrupt()`, you'll get an inline prompt:

```
▣ Background task: general-purpose-bg  (cli/general-purpose-bg#bg)
  Should I proceed with X?
  options: approve / reject
> 
```

Type your answer and hit Enter; the run resumes.

> **Tip — force a background delegation:** ask the master something it can
> reasonably split, e.g. *"organise my Downloads folder in the background and
> in the meantime tell me about the latest news"*. The system prompt
> explicitly tells the master to prefer `start_async_task` for long-running
> work — see [`yuyutsava/core/prompts.py`](../yuyutsava/core/prompts.py)
> (`ASYNC_SUBAGENT_GUIDANCE`).

---

## 3. Daemon — full stack with Electron renderer

### 3a. Boot the daemon

```bash
export YUYUTSAVA_ASYNC_SUBAGENTS=1
uv run yuyutsava daemon
```

Watch for these lines in the boot log:

```
async subs: enabled (YUYUTSAVA_ASYNC_SUBAGENTS=1)
async host: http://127.0.0.1:<port> (graphs=['file-organizer', 'face-watcher', 'general-purpose'])
async watcher: running
```

If you see `async subs: disabled (set YUYUTSAVA_ASYNC_SUBAGENTS=1 to enable)`,
the env var wasn't picked up.

### 3b. Launch the Electron renderer

In a separate shell:

```bash
cd electron-app
npm install     # first run only
npm run dev
```

The window opens and connects to the daemon's SSE stream. New on screen:

* **Right rail, above the Activity log:** a `BACKGROUND TASKS` section.
  Empty state shows `background tasks — none`.

### 3c. Trigger a background task

Use the daemon's normal entry points (web UI chat, voice command, or whatever
your daemon is wired to). Ask for something long-running with a phrase like
*"in the background"* or *"keep chatting while it runs"*.

In the Electron window you should see:

1. A new **TaskRow** appear under `BACKGROUND TASKS`:
   ```
   file-organizer-bg  task=a3f1c92e
   Organise Downloads
                               running   12s
   ```
2. If the bg graph hits an `interrupt()` (e.g. permission for `tr_write_file`),
   the existing `AskCard` shows with a new **`Background`** badge next to the
   `Permission` chip. Approve or reject as usual — the run resumes.
3. When the task completes the row flips green (`done`) and you get a focus-
   aware OS banner (the existing `notify:show` IPC, wired through
   `useSSE.jsx`).

### 3d. The master sees the result on its next turn

When you next chat with the orchestrator, the **first thing it gets in
context** is the "in-flight tasks" status block injected by
`_run_task` from the daemon-scoped `AsyncTaskMirror`. For tasks that
recently completed, the master will briefly acknowledge them before
answering your new question. You should see this in the chat history.

---

## 4. CLI Mode 2 — attach a terminal to the running daemon

The Electron renderer is your "TV"; `yuyutsava attach` is your "remote".
Run it in a separate terminal **while the daemon is running** (§3a).

### 4a. Attach

```bash
uv run yuyutsava attach --label my-laptop --session-id my-session-1
```

Expected on stderr:

```
attached to http://127.0.0.1:8765  channel=cli-remote  newly_attached=True
(Ctrl-C to detach)
```

The terminal now tails the daemon's event stream. You'll see timeline
lines, tool calls, log messages, etc. — same data the Electron renderer is
showing.

### 4b. HITL routing

If a Tier-2 ask (permission prompt or a `#bg` background subagent question)
comes in **for a session whose origin was tagged `my-session-1`**, the daemon
routes it to the attached CLI *first* — you'll see:

```
▣ Permission: WRITE [background]  (ask=552f403d)
  from: orchestrator/file-organizer-bg#bg
  WRITE /Users/.../Downloads/file.tmp
  zone: WORKSPACE  risk: medium

  Are you sure? This file looks important.
  options: approve / reject
> 
```

Type the answer and press Enter; the CLI POSTs it back to the daemon
(`/ask/{ask_id}/respond`), the watcher resumes the run, and the Electron
renderer also reflects the resolution.

Asks for *other* sessions (web UI's own session, for example) still go to
the Electron renderer — only your session's HITL is preferred to the CLI.

### 4c. Detach

`Ctrl-C` in the attached terminal. You'll see:

```
detached
```

The daemon removes the `CliRemoteChannel` from its router and clears your
session's entry in `SessionOriginMap`. Subsequent asks for that session
fall back to the default (web first, then terminal).

---

## 5. Test the polling tools manually

While at least one bg task is running, the master orchestrator (or you,
in a Python shell) can use the supervisor tools deepagents wires onto the
master:

```
start_async_task(subagent_type, description)   # returns task_id immediately
check_async_task(task_id)                       # one task's status + result
update_async_task(task_id, new_instruction)     # interrupt + restart
cancel_async_task(task_id)                      # stop a task
list_async_tasks(status_filter=None)            # snapshot of all tasks
```

These are documented in deepagents'
[`ASYNC_TASK_SYSTEM_PROMPT`](https://docs.langchain.com/oss/python/deepagents/async-subagents).
The master sees them automatically — there's no extra config on our side.

---

## 6. Quick troubleshooting

| Symptom | Likely cause | Where to look |
|---|---|---|
| `async subs: disabled` in daemon log | env var not exported | `echo $YUYUTSAVA_ASYNC_SUBAGENTS` |
| `AsyncSubagentHost: /ok healthcheck failed` | port collision or `langgraph-cli[inmem]` not installed | `uv pip show langgraph-cli`; the optional `[inmem]` extra brings in `langgraph-runtime-inmem` and `langgraph-api` |
| Background task stays `running` forever and never completes | watcher not started, or no `AsyncTaskMirror` wired through | check daemon log for `async watcher: running`; in CLI Mode 1 confirm `agent_stack.py` built the `cli_watcher` |
| Electron `BACKGROUND TASKS` section empty even when daemon shows the task running | SSE wire missing the `async_task_*` kinds | open Electron DevTools → Network → `/stream` and watch for `event: event` frames with `kind: async_task_started` |
| `yuyutsava attach` returns 503 | daemon not running or wrong URL | confirm `curl http://127.0.0.1:8765/health` returns 200 |
| Ask shows in both Electron renderer **and** CLI | by design — `WebHub.broadcast` fans out to every subscriber. First responder wins; the loser sees "ask expired or already answered" if it tries to respond | nothing to fix |
| CLI gets the ask but Electron doesn't | check `agent_path` ends with `#bg` and the `[background]` badge logic in `AskCard.jsx` |
| Bg task status block missing at the start of master turns | `OrchestratorDeps.async_task_mirror` is `None` | re-check the env var + daemon boot log |

---

## 7. Where the code lives

* Plan + design notes: `$HOME/.claude/plans/i-want-you-to-immutable-phoenix.md`
* Backend Python: `yuyutsava/async_subagents/` (host, mirror, watcher, cap, remote, session_origin)
* CLI side: `yuyutsava/cli/async_hitl.py`, `yuyutsava/cli/remote_attach.py`, `yuyutsava/cli/commands/attach.py`
* Daemon side: `yuyutsava/daemon/cli_remote_channel.py`, `yuyutsava/daemon/web/routers/cli_attach.py`
* Electron: `electron-app/src/renderer/components/background-tasks/`, modified `proposals/AskCard.jsx`, `hooks/useSSE.jsx`
* Tests: `test/async_subagents/test_unit.py`, `test/async_subagents/e2e_stack.py`
