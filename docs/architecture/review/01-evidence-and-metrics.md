# 01 — Evidence & Metrics

Raw measurements, with the command that produced each. **No interpretation here**
— every number is cited by a finding in `02`–`04`. This document exists so the
findings can be re-verified after any change, and so drift can be tracked over
time.

Measured on branch `yuyutsava-daemon` @ `f3fa86d`. All commands run from the
repository root.

---

## M1 — Size and shape

```
Python modules (excl. __pycache__)   350
Total lines                       55,601
Top-level packages                    35
```

```bash
find yuyutsava -name "*.py" -not -path "*__pycache__*" | wc -l
find yuyutsava -name "*.py" -not -path "*/__pycache__*" -exec wc -l {} + | tail -1
```

### Largest modules

| Lines | Module |
|-------|--------|
| 1319 | `yuyutsava/core/engine.py` |
| 1217 | `yuyutsava/daemon/bootstrap.py` |
| 1122 | `yuyutsava/todoboard/store.py` |
| 1050 | `yuyutsava/daemon/web/routers/converse.py` |
| 1030 | `yuyutsava/async_subagents/watcher.py` |
| 968 | `yuyutsava/cli/commands/chat_repl.py` |
| 866 | `yuyutsava/storage/pg/migrations.py` |
| 818 | `yuyutsava/core/streaming.py` |
| 816 | `yuyutsava/core/config.py` |
| 776 | `yuyutsava/agents/task_runner/tools.py` |

```bash
find yuyutsava -name "*.py" -not -path "*/__pycache__*" -exec wc -l {} + | sort -rn | head -11
```

---

## M2 — Function complexity

Measured with an AST pass: length in lines, a cyclomatic-style branch count
(`If`/`For`/`While`/`ExceptHandler`/`BoolOp`/`IfExp` nodes), and parameter count
(positional + keyword-only).

```
Functions over 100 lines        30
Functions over 200 lines        12
Functions with over 10 params   16
```

### Longest functions

| Lines | Branches | Params | Function |
|-------|----------|--------|----------|
| 927 | 58 | 1 | `build_daemon` — `yuyutsava/daemon/bootstrap.py:291` |
| 740 | **116** | 1 | `converse` — `yuyutsava/daemon/web/routers/converse.py:311` |
| 567 | 11 | 3 | `bind_tools` — `yuyutsava/agents/task_runner/tools.py:210` |
| 330 | 38 | 13 | `run_chat_repl` — `yuyutsava/cli/commands/chat_repl.py:639` |
| 321 | 32 | **32** | `build_cli_deepagent` — `yuyutsava/core/engine.py:431` |
| 270 | 12 | 17 | `build_agent_stack` — `yuyutsava/cli/agent_stack.py:131` |
| 258 | 13 | 3 | `make_todo_tools` — `yuyutsava/todoboard/tools.py:123` |
| 246 | 23 | **30** | `build_tinker_agent` — `yuyutsava/core/engine.py:1003` |
| 242 | 37 | 8 | `build_orchestrator` — `yuyutsava/core/engine.py:759` |
| 228 | 54 | 11 | `astream_agent_iter` — `yuyutsava/core/streaming.py:363` |
| 226 | 58 | 9 | `astream_agent` — `yuyutsava/core/streaming.py:593` |
| 209 | 33 | 1 | `_async_main` — `yuyutsava/daemon/main.py:331` |

### Highest parameter counts

| Params | Function |
|--------|----------|
| 32 | `build_cli_deepagent` — `core/engine.py:431` |
| 30 | `build_tinker_agent` — `core/engine.py:1003` |
| 25 | `create_app` — `daemon/web/app.py:73` |
| 25 | `make_app` — `daemon/web/server.py:14` |
| 17 | `build_agent_stack` — `cli/agent_stack.py:131` |
| 16 | `ConversationManager.__init__` — `daemon/conversation_manager.py:73` |
| 14 | `run_chat` — `cli/commands/chat.py:30` |
| 14 | `OrchestratorLoop.__init__` — `daemon/orchestrator_loop.py:98` |

<details>
<summary>Reproduce (AST pass)</summary>

```python
import ast, pathlib
rows = []
for p in pathlib.Path('yuyutsava').rglob('*.py'):
    if '__pycache__' in str(p): continue
    try: t = ast.parse(p.read_text(encoding='utf-8'))
    except Exception: continue
    for n in ast.walk(t):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ln = getattr(n, 'end_lineno', n.lineno) - n.lineno + 1
            cx = sum(1 for x in ast.walk(n) if isinstance(
                x, (ast.If, ast.For, ast.While, ast.ExceptHandler, ast.BoolOp, ast.IfExp)))
            args = len(n.args.args) + len(n.args.kwonlyargs)
            rows.append((ln, cx, args, f"{p}:{n.lineno}", n.name))
rows.sort(reverse=True)
for r in rows[:25]: print(r)
print("over100:", sum(1 for r in rows if r[0] > 100))
print("over200:", sum(1 for r in rows if r[0] > 200))
print("params>10:", sum(1 for r in rows if r[2] > 10))
```
</details>

---

## M3 — Storage topology

The central structural measurement of this review.

```
Classes named *Store                        71
ABC store interfaces                        17
Sqlite* implementations                     20
Pg* implementations                         21
Modules containing CREATE TABLE             16  (plus a separate 866-line migrations.py)
Direct `pg_pool is not None` branches
  inside build_daemon                       13
```

### The 17 domain triples

Each row is one domain implemented three times (interface + two backends).

| Domain | Interface | SQLite impl | Postgres impl |
|--------|-----------|-------------|---------------|
| Artifacts | `context/artifacts.py:96` | `:145` | `:209` |
| Thread summaries | `context/summary_store.py:34` | `:51` | `:117` |
| Transcripts | `context/transcript_store.py:82` | `:113` | `:203` |
| Memory | `memory/store.py:62` | `:195` | `:88` |
| Skills | `skills/store.py` | `:140` | `:67` |
| Todo board | `todoboard/store.py:47` | `:160` | `:700` |
| Visuals | `visuals/store.py` | `:94` | `:222` |
| Voice messages | `storage/voice_store.py` | `:114` | `:217` |
| Feedback | `storage/feedback_store.py:57` | `:91` | `:207` |
| Interrupts | `storage/interrupts.py:32` | `:57` | `:309` |
| Tasks | `daemon/task_registry.py` | `:132` | `:242` |
| Usage | `daemon/usage.py` | `:141` | `:211` |
| Events | `storage/events/abc.py` | `sqlite_backend.py:115` | `pg_stores.py:46` |
| Proposals | `storage/events/abc.py` | `:160` | `:97` |
| Decisions | `storage/events/abc.py` | `:187` | `:138` |
| Consent rules | `storage/events/abc.py` | `:230` | `:200` |
| Consent grants | `storage/events/abc.py` | `:310` | `:288` |
| Tool counters | `storage/events/abc.py` | `:247` | `:231` |
| Pending asks | `storage/events/abc.py` | `:405` | `:327` |
| Sessions | `storage/sessions/store.py` (Protocol) | `sqlite_impl.py:36` | `pg_impl.py:50` |

```bash
grep -rn "^class .*Store" yuyutsava --include="*.py" | grep -v __pycache__
grep -rln "CREATE TABLE" yuyutsava --include="*.py" | grep -v migrations
grep -c "pg_pool is not None\|pg_pool else\|if pg_pool" yuyutsava/daemon/bootstrap.py
```

### Twin divergence sample

The same business rule — *"update the note, then touch the parent card's
`updated_ts`, then return the note"* — written twice:

- `SqliteTodoStore.assign_note` — `yuyutsava/todoboard/store.py:470-497`
  (28 lines; wraps in `_run_write` → `BEGIN IMMEDIATE`; re-SELECTs to find `card_id`)
- `PgTodoStore.assign_note` — `yuyutsava/todoboard/store.py:904-930`
  (27 lines; `async with pool.connection()`; uses `RETURNING card_id`)

Line-level textual similarity across the full twin pairs, for reference:

| Pair | Similarity |
|------|-----------|
| `SqliteTodoStore` vs `PgTodoStore` | 37.9% |
| `events/sqlite_backend.py` vs `events/pg_stores.py` | 22.5% |
| `astream_agent_iter` vs `astream_agent` | 34.1% |

> **Reading these numbers correctly:** low textual similarity is *not* evidence
> against duplication here. The duplication is **semantic and structural** — the
> same method list, the same order, the same contracts, the same business rules —
> expressed in two SQL dialects with different row-access idioms. Textual
> diffing understates it; the method-by-method correspondence in the table above
> is the real measurement.

---

## M4 — Coupling and the import graph

```
Internal imports at module top level        709
Internal imports deferred inside functions  220   (23.7%)
Modules importing langchain/langgraph/deepagents  64 of 350  (18.3%)
Comments explicitly citing import cycles     15
```

### Modules with the most deferred imports

| Deferred | Module |
|----------|--------|
| 68 | `yuyutsava/core/engine.py` |
| 19 | `yuyutsava/cli/agent_stack.py` |
| 17 | `yuyutsava/daemon/bootstrap.py` |
| 13 | `yuyutsava/cli/commands/chat_repl.py` |
| 7 | `yuyutsava/agents/tinker/agent.py` |

### Highest fan-in (most depended-upon internal modules)

| Importers | Module |
|-----------|--------|
| 42 | `yuyutsava.core.config` |
| 25 | `yuyutsava.storage.paths` |
| 20 | `yuyutsava.storage.pg.pool` |
| 20 | `yuyutsava.storage.events` |
| 17 | `yuyutsava.daemon.channels` |
| 14 | `yuyutsava.storage.base` |
| 13 | `yuyutsava.skills.registry` |
| 10 | `yuyutsava.core.engine` |

```bash
grep -rnE "^\s+(from|import) yuyutsava" yuyutsava --include="*.py" | grep -v __pycache__ | wc -l
grep -rnE "^(from|import) yuyutsava"    yuyutsava --include="*.py" | grep -v __pycache__ | wc -l
grep -rhoE "^(from|import) yuyutsava[.a-z_0-9]*" yuyutsava --include="*.py" \
  | sed 's/^from //;s/^import //' | sort | uniq -c | sort -rn | head -25
```

### Type erasure at dependency seams

```
Modules using `: Any` / `Any | None` in signatures — top offender: core/engine.py (48)
Dependency fields typed `object | None` on OrchestratorDeps       10
getattr() calls across the package                               200
getattr(deps, ...) defensive reads of declared fields             15
```

`getattr(deps, "async_subagents", None)` at `core/engine.py:909` reads a field
that **is** declared on the dataclass (`agents/orchestrator/agent.py:62`) — the
declared contract is not trusted by its own consumer.

---

## M5 — Third-party surface area

```
Modules subclassing langchain AgentMiddleware   14
Modules importing langchain_core.messages       20
Modules importing langchain_core.tools          23
Modules importing langchain_core.language_models 18
Modules referencing BaseChatModel               18
create_deep_agent() call sites                   4  (all in core/engine.py)
Direct deepagents importers                      3
Dependencies with an upper version bound         0
```

### The 14 middleware classes

All extend `langchain.agents.middleware.AgentMiddleware` directly:

`PromptInspectorMiddleware`, `TranscriptRecorderMiddleware`,
`SubagentGateMiddleware`, `ToolFilterMiddleware`, `ToolResultOffloadMiddleware`,
`VoiceStyleMiddleware`, `FilesystemPromptOverrideMiddleware`,
`RetrievalInjectionMiddleware`, `PermissionMiddleware`, `BudgetMiddleware`,
`UsageRecorder`, `CheckAsyncTaskGuardMiddleware`, `BackgroundTaskCapMiddleware`,
`AsyncTaskInterruptPatchMiddleware`.

`PermissionMiddleware` carries `# type: ignore[misc]` on its class statement
(`core/permission_middleware.py:254`).

### Framework-internal dependencies

| Site | What it depends on | Stability |
|------|--------------------|-----------|
| `core/filesystem_prompt_middleware.py:43` | `from deepagents.middleware.filesystem import FILESYSTEM_SYSTEM_PROMPT` | Undocumented constant; guarded by `try/except` |
| `core/filesystem_prompt_middleware.py:47` | String match on the literal `"## Filesystem Tools"` heading | Breaks silently on any rewording |
| `agents/general_purpose/agent.py:87` | Name-match override behavior documented as `deepagents/graph.py:240-246` | Line-numbered reference to library internals |
| `core/engine.py:483` | Same name-match behavior | Line-numbered reference |
| `core/docker_sandbox_backend.py:21,29` | `deepagents.backends.protocol`, `deepagents.backends.sandbox.BaseSandbox` | Subclasses a framework base class |

```bash
grep -rn "class .*(AgentMiddleware)" yuyutsava --include="*.py" | grep -v __pycache__
grep -rn "create_deep_agent(" yuyutsava --include="*.py" | grep -v __pycache__
grep -n "deepagents\|langchain\|langgraph" pyproject.toml
```

### Declared dependency floors (no ceilings anywhere)

```
deepagents>=0.6.3
langgraph-cli[inmem]>=0.4.27
langchain-openai>=1.1.11
langgraph-checkpoint-sqlite>=3.0.3
langgraph-checkpoint-postgres>=2.0.0
langchain-anthropic>=1.4        (extra)
langchain-google-genai>=4.2     (extra)
langchain-google-vertexai>=3.2  (extra)
langchain-aws>=1.6              (extra)
langchain-mistralai>=1.1        (extra)
langchain-cohere>=0.6           (extra)
```

---

## M6 — Dead and unapplied structure

| Observation | Evidence |
|-------------|----------|
| `yuyutsava/daemon/web/repositories/` contains no modules | Directory exists, is empty — an abandoned layer |
| `RoutedStore` is documented as serving "every domain twin — events, consent, interrupts, memory, skills" | `storage/routing/facade.py:10-11`; actually wired for 3 stores (visual, feedback, todo) at `bootstrap.py:428-443` |
| `BaseSqliteStore` docstring says "No store inherits from it yet" | `storage/base.py:10-12`; 12 stores now inherit from it — the docstring is stale, the class is used |
| `build_agent` back-compat alias kept "for one cycle" | `core/engine.py:756` |
| `spawn_subagent` tool built but deliberately never registered | `agents/orchestrator/spawn.py:125` (167 lines); non-registration noted at `core/engine.py:823` |

---

## Metric summary table

For tracking across future reviews.

| Metric | Value | Direction |
|--------|-------|-----------|
| Modules | 350 | — |
| Lines | 55,601 | — |
| Store classes | 71 | ↓ target ~35 |
| Functions > 200 lines | 12 | ↓ target 0 |
| Functions > 10 params | 16 | ↓ target ≤ 3 |
| Max cyclomatic | 116 | ↓ target ≤ 25 |
| Deferred-import ratio | 23.7% | ↓ target < 8% |
| Modules touching a framework directly | 64 (18.3%) | ↓ target < 25 |
| Dependencies with version ceilings | 0 | ↑ target = all framework deps |
| `object \| None` dependency fields | 10 | ↓ target 0 |
