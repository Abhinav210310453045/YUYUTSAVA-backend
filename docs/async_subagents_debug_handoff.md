# Async Subagents — Debug Handoff

Pick this up in a fresh chat to save tokens. Everything you need is below.

---

## TL;DR

The async-subagents implementation (host + watcher + mirror + channel routing
+ CLI Modes 1+2 + Electron UI + tests) is **fully built and tested in
isolation** (20 unit tests + 15 E2E checks pass). When booting the real
daemon, `langgraph-api` refused to load the subagent graphs because they
came pre-baked with a custom `AsyncSqliteSaver` checkpointer — and
`langgraph-api`'s `local_dev` validator rejects any graph carrying its own
persistence.

**Fix applied** to `yuyutsava/agents/base_sub_agent.py`:

```python
# Line 273-288, base_sub_agent.py
def build_async_graph(self, model, checkpointer) -> CompiledStateGraph:
    """No checkpointer is passed here: LangGraph API injects its own
    checkpointer at runtime, and embedding one causes a ValueError at
    graph load time."""
    return self.build_react_agent(model, None)
```

And the signature of `build_react_agent` was widened:

```python
# Line 179-183
def build_react_agent(
    self,
    model: BaseChatModel,
    checkpointer: BaseCheckpointSaver | None,   # was: required
) -> CompiledStateGraph: ...
```

**Status**: Fix is **on disk** but the daemon hasn't been re-run since the
fix landed. The failing log you saw is from **before** the fix.

---

## What to do next (~5 minutes)

1. **Re-run the daemon** with the env var set:

   ```bash
   cd $REPO
   export YUYUTSAVA_ASYNC_SUBAGENTS=1
   uv run yuyutsava daemon
   ```

2. **Expect to see** (no more ValueError about AsyncSqliteSaver):

   ```
   async subs: enabled (YUYUTSAVA_ASYNC_SUBAGENTS=1)
   - 🚀 API: http://127.0.0.1:<port>
   …Importing graph profiling   graph_id=file-organizer …
   …Importing graph profiling   graph_id=face-watcher …
   …Importing graph profiling   graph_id=general-purpose …
   …Application started up in N.NNNs
   async host: http://127.0.0.1:<port> (graphs=['file-organizer', 'face-watcher', 'general-purpose'])
   async watcher: running
   ```

3. **If the daemon still fails the same way**, your `__pycache__` may be
   stale. Clear it and retry:

   ```bash
   find yuyutsava -type d -name __pycache__ -exec rm -rf {} +
   uv run yuyutsava daemon
   ```

4. **If it succeeds**, you're done — follow [`docs/async_subagents_runbook.md`](async_subagents_runbook.md) to exercise the Electron renderer + `yuyutsava attach`.

---

## Why I'm confident the fix works

Three independent confirmations:

### A. The on-disk fix is correct

```
$ grep -n "return self.build_react_agent" yuyutsava/agents/base_sub_agent.py
288:        return self.build_react_agent(model, None)
```

### B. The langgraph API behaves as expected when checkpointer=None

I ran a minimal repro (no yuyutsava imports):

```python
from langgraph.prebuilt import create_react_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel

g = create_react_agent(model=FakeListChatModel(responses=["ok"]), tools=[], checkpointer=None)
print(g.checkpointer)   # → None
```

`graph.checkpointer` is `None` — exactly what `langgraph-api`'s validator
(at `langgraph_api/graph.py:790`) needs to pass.

### C. File mtimes prove the failure was pre-fix

```
$ ls -la yuyutsava/agents/base_sub_agent.py
-rw-r--r-- … May 27 13:17 yuyutsava/agents/base_sub_agent.py

failing daemon log timestamp:  13:15:10
```

The fix was saved 2 minutes after the daemon failed.

---

## If it still fails after re-running

Drop these two paragraphs into the new chat, with whatever fresh log output
you have:

> I'm continuing from `docs/async_subagents_debug_handoff.md`. I re-ran
> `uv run yuyutsava daemon` with `YUYUTSAVA_ASYNC_SUBAGENTS=1` after the
> `build_async_graph(model, None)` fix in `base_sub_agent.py:288`. Output
> below. The previous chat verified `create_react_agent(checkpointer=None)`
> produces `graph.checkpointer=None` in isolation, so if the validator
> still rejects, the leak is somewhere else.

> Investigate: (1) is the path actually `build_async_graph` being called?
> Add a print at line 288 to confirm. (2) Does any subagent
> (FileOrganizerAgent / FaceWatcherAgent / GeneralPurposeAgent) override
> `build_react_agent` or `build_async_graph`? (3) Does
> `AsyncSubagentHost.from_subagents` mutate the graph after compile? (4) Is
> there a `__pycache__` shadowing the new source?

---

## File map (for context)

The bulk of the work lives under these paths. If you need to inspect any
piece:

- **The fix**: [`yuyutsava/agents/base_sub_agent.py`](../yuyutsava/agents/base_sub_agent.py) (lines 179, 273–288)
- **Host (in-process langgraph server)**: [`yuyutsava/async_subagents/host.py`](../yuyutsava/async_subagents/host.py)
- **Module bridge** (graphs registered via `globals()`): [`yuyutsava/async_subagents/_lg_graphs.py`](../yuyutsava/async_subagents/_lg_graphs.py)
- **Mirror / watcher / cap / remote / origin**: [`yuyutsava/async_subagents/`](../yuyutsava/async_subagents/)
- **Daemon wiring**: [`yuyutsava/daemon/bootstrap.py`](../yuyutsava/daemon/bootstrap.py) lines 286–360 (env-gated on `YUYUTSAVA_ASYNC_SUBAGENTS=1`)
- **CLI Mode 1 (standalone)**: [`yuyutsava/cli/async_hitl.py`](../yuyutsava/cli/async_hitl.py), [`yuyutsava/cli/agent_stack.py`](../yuyutsava/cli/agent_stack.py)
- **CLI Mode 2 (attach to daemon)**: [`yuyutsava/cli/remote_attach.py`](../yuyutsava/cli/remote_attach.py), [`yuyutsava/cli/commands/attach.py`](../yuyutsava/cli/commands/attach.py), [`yuyutsava/daemon/cli_remote_channel.py`](../yuyutsava/daemon/cli_remote_channel.py), [`yuyutsava/daemon/web/routers/cli_attach.py`](../yuyutsava/daemon/web/routers/cli_attach.py)
- **Electron UI**: [`electron-app/src/renderer/components/background-tasks/`](../electron-app/src/renderer/components/background-tasks/), [`electron-app/src/renderer/components/proposals/AskCard.jsx`](../electron-app/src/renderer/components/proposals/AskCard.jsx) (`#bg` badge), [`electron-app/src/renderer/hooks/useSSE.jsx`](../electron-app/src/renderer/hooks/useSSE.jsx)
- **Tests**: [`test/async_subagents/test_unit.py`](../test/async_subagents/test_unit.py) (20 unit), [`test/async_subagents/e2e_stack.py`](../test/async_subagents/e2e_stack.py) (15 E2E)
- **Plan**: [`$HOME/.claude/plans/i-want-you-to-immutable-phoenix.md`](../../.claude/plans/i-want-you-to-immutable-phoenix.md)
- **Runbook**: [`docs/async_subagents_runbook.md`](async_subagents_runbook.md)

---

## Why the diagnostic in the previous chat kept hanging

`uv run python -c "..."` invocations that import `GeneralPurposeAgent`
trigger a heavy chain: MCP client manager, skill registry, search providers,
the full TaskRunner stack. On this Mac it took >60s and the bash tool
auto-backgrounded each attempt, then auto-killed them on timeout.

The minimal repro (case B above) skipped all of that and completed in 2s.
For future debugging of this stack, prefer tests that import only what's
strictly necessary.
