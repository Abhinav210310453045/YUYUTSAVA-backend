# Persistent CLI Sessions — Implementation Handoff

This document hands off the in-progress "wait forever, persist everything" feature so another Claude session (or engineer) can pick it up cold.

The plan source-of-truth lives at `/Users/abhinav0087/.claude/plans/i-want-you-to-snug-globe.md`. Read it once. This file tells you **what shipped in PR #1 (MVS)** and **what PRs #2–#4 still need to do**.

---

## 0. Mental model in one paragraph

The CLI used to lose every conversation when the process died (`MemorySaver()`). PR #1 swapped that for a SQLite checkpointer + a separate `sessions` table that indexes each run with metadata (workspace, status, msg count, etc.). The CLI now has `--list-sessions`, `--resume <id>`, `--continue` flags. The architecture is intentionally pluggable — there is a `SessionStore` Protocol so a future Postgres backend (PR #4) is a single-factory swap. PR #2 adds the Electron UI panel. PR #3 hardens (TTL sweeper, pagination, delete command).

---

## 1. PR #1 — MVS (✅ SHIPPED)

### What exists now

**New package `yuyutsava/sessions/`** — every file is < 250 LoC, single-responsibility:

| File | Responsibility |
|---|---|
| `models.py` | Frozen `Session` dataclass. Statuses: `running`, `idle`, `crashed`, `done`. |
| `store.py` | `SessionStore` Protocol + `SessionNotFound`. **The contract every backend implements.** |
| `sqlite_store.py` | `SqliteSessionStore` (aiosqlite, WAL, BEGIN IMMEDIATE retry, per-process asyncio lock). `mint_thread_id("cli")` returns `cli-<unix_ts>-<uuid4>` — sweeper-compatible with `yuyutsava/daemon/checkpointing.py:39`. `get_default_session_store()` is a process-singleton factory. |
| `checkpointer.py` | `build_checkpointer(settings)` async context-manager → `BaseCheckpointSaver`. Today only `backend == "sqlite"`; raises `NotImplementedError` for anything else (PR #4 hook). |
| `config.py` | `SessionsSettings.from_env()`. Knobs: `db_path`, `backend`, `busy_timeout_ms`. |
| `runner.py` | `run_session(...)` — crash-safe wrapper around `astream_agent`. Owns row create / coalesced touch / status flip in `finally:`. Also exports `ResumeFailed` for CLI-level error handling. |
| `__init__.py` | Re-exports the public surface. |

**Modified files** (mind the contract — don't break callers):

| File | Change |
|---|---|
| `yuyutsava/core/config.py` | Added `sessions_db_path()` next to `yuyutsava_home()`. Env override `YUYUTSAVA_SESSIONS_DB`. Default `~/.yuyutsava/sessions.db`. |
| `yuyutsava/core/engine.py` | `build_agent(..., checkpointer: BaseCheckpointSaver \| None = None)` — defaults to `MemorySaver()` if not passed, so the graph-export branch + tests stay green. `astream_agent(..., on_tick=None)` — awaited with `(steps: int)` after each agent.astream pass, used by the runner for coalesced bookkeeping. |
| `yuyutsava/cli/cli.py` | New flags `--list-sessions` (shows ALL workspaces by default), `--this-workspace` (opt-in filter), `--resume <ID>`, `--continue`. New helper `_print_sessions_table()`. Main flow now opens `build_checkpointer(SessionsSettings.from_env())` and calls `run_session(...)` instead of `astream_agent` directly. `ResumeFailed` caught and printed cleanly with exit 2. |

**Tests** (`uv run python -m unittest test.sessions.test_sqlite_store test.sessions.test_runner_crash`):

- `test/sessions/test_sqlite_store.py` — 9 tests: roundtrip, missing-id, touch counters, memory_files set, status validation, list ordering + workspace filter, delete, concurrent touches (20 in parallel land all writes), thread-id format.
- `test/sessions/test_runner_crash.py` — 4 tests: row exists before first step, simulated crash leaves recoverable row, `--continue` picks newest, resume re-marks running.

All 13 currently pass.

### Storage layout

One SQLite file: `~/.yuyutsava/sessions.db` (override via `YUYUTSAVA_SESSIONS_DB`). Two tables:

- `sessions` — the index we own (id, thread_id, workspace, status, created_at, updated_at, message_count, memory_files_count, db_row_bytes, task_preview, schema_version).
- `checkpoints` / `writes` — owned by LangGraph's `AsyncSqliteSaver`, populated automatically when the agent runs.

`SqliteSessionStore.touch()` reads `SUM(LENGTH(checkpoint)+LENGTH(metadata)) FROM checkpoints WHERE thread_id=?` to populate `db_row_bytes`. Defensively wrapped in try/except — if the checkpoints table doesn't exist yet (e.g. fresh DB before first run), we record 0 instead of crashing.

### Concurrency model

- WAL + `busy_timeout=5000` permits concurrent readers (daemon polling) while CLI writes.
- Each mutation: `BEGIN IMMEDIATE` with up to 3 retries on `SQLITE_BUSY`.
- Per-process `asyncio.Lock` in `SqliteSessionStore._run_write` serializes within-process writers.
- CLI is the only process that opens `AsyncSqliteSaver` for a given active thread; daemon should read-only from `sessions` table.

### Smoke commands that work today

```bash
uv run yuyutsava --help                                        # new flags visible
uv run yuyutsava --list-sessions                               # empty → friendly msg
YUYUTSAVA_SESSIONS_DB=/tmp/x.db uv run yuyutsava --list-sessions
uv run yuyutsava --resume unknown-id "task"                    # exit 2 with clean error
uv run python -m unittest test.sessions.test_sqlite_store -v   # 9 ok
uv run python -m unittest test.sessions.test_runner_crash -v   # 4 ok
```

---

## 2. PR #2 — UI Sessions panel (NEXT)

**Goal:** Add a Sessions view to the Electron app that lists rows from `sessions.db` and lets the user copy a `--resume` command. No backend session logic changes — this PR is daemon route + React panel only.

### Backend (daemon)

**Create** `yuyutsava/daemon/web/routers/sessions.py`:
- `GET /sessions` — optional `?workspace=<path>` and `?limit=<n>` query params. Returns JSON list shaped like `Session` (workspace as string, timestamps as ISO 8601 OR raw float — pick one; whichever the UI parses easier).
- `GET /sessions/{id}` — single row or 404.
- Use `Depends(get_session_store)` where `get_session_store` calls `yuyutsava.sessions.get_default_session_store()` (it's already a process-singleton).

**Create** `yuyutsava/daemon/web/schemas/session.py`:
- `SessionOut` pydantic model mirroring `Session`. Pay attention to `workspace: str` (Path doesn't serialize cleanly).

**Modify** `yuyutsava/daemon/web/app.py`:
- Add `from yuyutsava.daemon.web.routers import sessions as sessions_router`
- `app.include_router(sessions_router.router)` next to the existing routers (proposals, decisions, rules, skills, config, health, stream, static_files).

**No** changes to `yuyutsava/daemon/main.py` — `get_default_session_store()` is process-singleton; no app.state wiring needed.

### Frontend (Electron + React)

The Electron UI lives at `electron-app/`. React 18 + Vite 5 + custom CSS theme (neon green, terminal aesthetic).

**Modify** `electron-app/src/renderer/api/client.js`:
- Add `export async function listSessions(workspace) { return _json('GET', '/sessions' + (workspace ? '?workspace=' + encodeURIComponent(workspace) : '')) }`
- Add `export async function getSession(id) { return _json('GET', '/sessions/' + encodeURIComponent(id)) }`

**Modify** `electron-app/src/renderer/components/layout/Sidebar.jsx`:
- Add an SVG icon to the `icons` map (line 3-21) under key `sessions`. Suggested icon: clock or stacked-layers shape. Use the same `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" ...>` pattern as the existing icons.
- Add `{ id: 'sessions', label: 'Sessions' }` to the `items` array at line 24.

**Modify** `electron-app/src/renderer/App.jsx`:
- Add an `activePanel === 'sessions'` branch in the conditional rendering around line 107-116, similar to how `proposals` and `settings` are rendered.
- **Important VS-Code-style toggle:** change the `onNav` handler so clicking the active panel collapses back to chat: `setActivePanel(p => p === id ? 'chat' : id)`. The plan calls this out as a UX requirement.

**Create** `electron-app/src/renderer/components/sessions/SessionsPanel.jsx`:
- Mirror the structure of `electron-app/src/renderer/components/proposals/ProposalsPanel.jsx`: heading with count badge (monospace, neon green), scrollable list, empty state, gap between cards.
- Use `useEffect` + `setInterval(5000)` to poll `listSessions(currentWorkspace)`. Clear interval on unmount.
- Empty state: "No sessions yet — run `uv run yuyutsava <task>` in your terminal."
- Use theme tokens from `electron-app/src/renderer/styles/theme.css` (`--bg-card`, `--neon-green`, `--font-mono`, etc.).

**Create** `electron-app/src/renderer/components/sessions/SessionRow.jsx`:
- Props: `session` (the row), and that's it.
- Show:
  - Short id (first 8 chars + ellipsis; full id on hover via `title=`)
  - Status dot (running=green, idle=amber, crashed=red, done=grey)
  - Workspace basename + full path tooltip
  - Pills: `msgs: N`, `mem: N`, formatted bytes (use `_human_bytes` logic from `yuyutsava/cli/cli.py` — copy it to JS)
  - Relative "updated_at" ("3m ago" — mirror `_human_age` logic)
  - `[Copy resume]` button → `navigator.clipboard.writeText(...)` of the string:
    ```
    uv run yuyutsava --verbose --workspace <workspace> --resume <id>
    ```
- Visual feedback on copy: briefly swap button label to "Copied!" for 1.5s.

### Acceptance for PR #2

1. Start daemon: `uv run yuyutsava daemon`
2. Open the Electron app.
3. Click "Sessions" in the sidebar → list appears.
4. Click "Sessions" again → panel collapses back to chat.
5. After running a CLI session, refresh → new row appears (poll catches it within 5s).
6. Click "Copy resume" → paste into a terminal → it runs and resumes correctly.

---

## 3. PR #3 — Hardening (FOLLOW-UP)

Small bag of operational improvements that aren't required for the feature to work but make it production-grade.

### TTL sweeper for the `sessions` table

Mirror `yuyutsava/daemon/checkpointing.py:127-167` (the existing checkpointer sweeper). Run as a background task in the daemon. Delete rows whose `updated_at` is older than `ttl_sec` (default 7 days; longer than checkpointer TTL because session rows are tiny).

- New module: `yuyutsava/sessions/sweeper.py`
- Wire from `yuyutsava/daemon/main.py` similar to `CheckpointerManager`.

### `--delete-session <id>` CLI flag

- Add to `_build_parser()` in `yuyutsava/cli/cli.py`.
- Short-circuit before `build_agent` (no LLM needed).
- Calls `store.delete(id)`. Also delete the corresponding checkpoint via `AsyncSqliteSaver.adelete_thread(id)` — sessions and checkpoints should be deleted together. The cleanest way: open the saver briefly, call adelete_thread, then call store.delete.

### Pagination for `GET /sessions`

- Add `?cursor=<updated_at>&limit=<n>` to the daemon route.
- `SqliteSessionStore.list()` already takes `limit`; add a `cursor` param (filter `WHERE updated_at < ?`).

### Misc

- `--all-workspaces` flag for `--list-sessions` (currently it always filters by current workspace).
- Persist `task_preview` updates across resumes so the most recent prompt is shown, not just the original (currently only set on `create`).

---

## 4. PR #4 — Postgres backend (LATER)

**Trigger:** when SQLite contention becomes a real problem (multi-machine deployments, hosted daemon, etc.).

**What changes:**

- New file `yuyutsava/sessions/postgres_store.py` implementing the `SessionStore` Protocol using SQLAlchemy Core + asyncpg.
- Add `AsyncPostgresSaver` branch in `yuyutsava/sessions/checkpointer.py:build_checkpointer` (langgraph ships this — `langgraph.checkpoint.postgres.aio.AsyncPostgresSaver`).
- Switch in `SessionsSettings.from_env`: `backend = "postgres"` reads `YUYUTSAVA_SESSIONS_PG_DSN`.
- `get_default_session_store()` dispatches on `settings.backend`.

**Important:** because every caller depends only on the `SessionStore` Protocol, **no other file changes**. That's the whole point of the abstraction — verify this stays true before merging.

Schema migration: the SQLite schema's `sessions_meta.schema_version` row is the migration anchor. For Postgres, either reuse the same convention or move to Alembic. The plan suggests not adding Alembic until Postgres lands; this is the moment.

---

## 5. Don't break these invariants

These were load-bearing decisions in PR #1. Future PRs must respect them.

1. **`SessionStore` is the only contract callers depend on.** No CLI or daemon code should import `SqliteSessionStore` by name (use `get_default_session_store()`). If you need to type-hint, use the `SessionStore` Protocol.
2. **`build_agent`'s `checkpointer` kwarg defaults to `None`** so existing callers (graph export, tests) keep working. Don't make it required.
3. **`run_session` creates the row BEFORE the first LLM call.** This is the entire crash-recovery guarantee. Never move that `store.create` after any await that might block on the model.
4. **`on_tick` is fire-and-forget bookkeeping.** Its exceptions are swallowed inside `astream_agent` (logged, not raised). Never make the agent loop's correctness depend on `on_tick` succeeding.
5. **Thread id format is `<role>-<unix_ts>-<uuid4>`.** The daemon's TTL sweeper parses this. If you change the format, update `yuyutsava/daemon/checkpointing.py:_parse_ts` too.
6. **Two writers, one SQLite file** — keep `BEGIN IMMEDIATE` + retry semantics in any new mutation method.

---

## 6. Useful file-path map

```
yuyutsava/
  cli/cli.py                         ← _build_parser, _print_sessions_table, main flow
  core/config.py                     ← sessions_db_path()
  core/engine.py                     ← build_agent(checkpointer=), astream_agent(on_tick=)
  daemon/checkpointing.py            ← READ for the sweeper pattern to mirror
  daemon/web/app.py                  ← include_router() goes here for PR #2
  daemon/web/routers/                ← PR #2 adds sessions.py here
  sessions/
    __init__.py                      ← public exports
    models.py                        ← Session dataclass
    store.py                         ← SessionStore Protocol + SessionNotFound
    sqlite_store.py                  ← SqliteSessionStore, mint_thread_id, get_default_session_store
    checkpointer.py                  ← build_checkpointer()
    config.py                        ← SessionsSettings
    runner.py                        ← run_session(), ResumeFailed, _CoalescedTicker

electron-app/
  src/renderer/
    api/client.js                    ← add listSessions, getSession (PR #2)
    App.jsx                          ← add 'sessions' panel branch + toggle (PR #2)
    components/
      layout/Sidebar.jsx             ← add 'sessions' icon + item (PR #2)
      proposals/ProposalsPanel.jsx   ← READ — mirror this structure for SessionsPanel
      sessions/                      ← create this dir in PR #2

test/sessions/
  test_sqlite_store.py               ← 9 unit tests (all pass)
  test_runner_crash.py               ← 4 crash-recovery tests (all pass)
```

---

## 7. Quick verification commands

```bash
# Unit tests
uv run python -m unittest test.sessions.test_sqlite_store test.sessions.test_runner_crash -v

# CLI surface
uv run yuyutsava --help | grep -A1 -E "list-sessions|resume|continue"
uv run yuyutsava --list-sessions
uv run yuyutsava --resume nope "x"     # → "Error: No session with id 'nope'..." exit 2

# Seed + list (useful for UI dev without a real LLM run)
YUYUTSAVA_SESSIONS_DB=/tmp/dev.db uv run python -c "
import asyncio
from pathlib import Path
from yuyutsava.sessions import get_default_session_store
async def main():
    s = get_default_session_store()
    a = await s.create(workspace=Path.cwd(), task='example task')
    await s.touch(a.id, message_delta=5, memory_files_count=2)
asyncio.run(main())"
YUYUTSAVA_SESSIONS_DB=/tmp/dev.db uv run yuyutsava --workspace . --list-sessions
```

---

## 8. Open questions / nice-to-haves not in the plan

These are deliberately out of scope but worth noting so you don't get blindsided:

- ~~**Resume semantics when LangGraph state has an unresolved interrupt.** Today `--resume <id> "<new task>"` always passes the new task as a HumanMessage. If the previous session was paused on a permission interrupt, the right behavior is `Command(resume=<decision>)` rather than a new message.~~ **Partially fixed in PR #1 follow-up:** `runner._patch_orphan_cancellations` runs on every resume. It finds LangGraph-generated cancellation `ToolMessage`s (status=success, content "was cancelled - another message came in...") and rewrites them in-place to `status=error` with an explicit DENIED message. This stops the model from hallucinating that the cancelled action succeeded. **What's still pending for a follow-up:** the user is never given a chance to *re-approve* the cancelled action — they just see it as denied and have to re-ask. Detecting actual pending interrupts in checkpoint state (vs. orphan messages) and re-prompting would close the gap.
- **Session ownership / multi-user.** Today there's no `user_id` column. Fine for a single-user CLI; revisit if the daemon goes multi-tenant.
- **`memory_files_count` semantics.** Currently counts `<workspace>/.skills/**/SKILL.md`. The plan called this "memory files" — the yuyutsava project doesn't have a memory system per se, so we used skill files as the closest proxy. If a real memory system lands, update `_count_memory_files` in `yuyutsava/sessions/runner.py`.
- ~~**CLI's `--list-sessions` defaults to current-workspace filter.** No way to see all sessions yet. Add `--all-workspaces` in PR #3.~~ **Fixed in PR #1 follow-up:** `--list-sessions` now shows all workspaces by default. Add `--this-workspace` to filter to the current `--workspace`.
