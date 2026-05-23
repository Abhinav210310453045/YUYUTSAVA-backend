# YUYUTSAVA Restructure — Chat Handoff

## What this is

A senior-architect restructure of the YUYUTSAVA codebase that puts persistence under a single `storage/` package, splits the 1,222-line `core/engine.py`, thins the CLI and daemon, and replaces `dict[str, Any]` payloads with typed models. The agent architecture (`BaseSubAgent` ABC, `engine.py` as the deepagent factory, `TaskRunnerAgent` permission gateway) is **not** being changed — it's already correct.

## Plan file

The full plan lives at:

```
$HOME/.claude/plans/i-want-you-to-nifty-starlight.md
```

A new chat session can `Read` it directly. It contains:
- §1 Findings — every issue verified with file:line refs
- §2 Target folder structure (full tree)
- §3 Storage consolidation details
- §4 Engine split details
- §5 CLI extraction details
- §6 Daemon trim details
- §7 Type safety + standards pass
- §8 Critical files modified (full list)
- §9 Suggested execution order (7 steps)
- §10 Verification recipes
- §11 Honest pushback — what NOT to do
- §12 What's well done and shouldn't change

## User-confirmed scope decisions

1. **Migration style:** Clean break — update all imports, no shims.
2. **Scope:** Full restructure — storage, engine, CLI, daemon, type safety.
3. **CLI shape:** Keep procedural, extract concerns into modules. **Don't** make it a class.

These are recorded in the plan but the next chat should honor them without re-asking.

---

## Status

| Step | Status | Description |
|---|---|---|
| **1. Foundations** | ✅ Complete | `storage/` package skeleton, path/id consolidation, magic constants, inline import cleanup |
| **2. Move storage modules** | ✅ Complete | Sessions/interrupts moved + inherit `BaseSqliteStore`; events store moved with typed reads; prefs moved & renamed `PrefsStore`; 18+ callers updated; dead code removed |
| **3. Move db_introspect + unify sweepers** | ✅ Complete | `storage/introspect.py` (typed `DatabaseInfo`), `storage/sweeper.py` (`UnifiedSweeper`), `CheckpointerSaver` slim lifecycle in daemon, 3 daemon files deleted |
| **4. Engine split** | ✅ Complete | `core/streaming.py`, `core/tool_result.py`, `core/prompts.py` extracted; `engine.py` 1222 → 408 lines; back-compat re-export deleted; dead `_print_token_usage` dropped |
| **5. CLI extraction** | ✅ Complete | `cli/commands/{chat,sessions,prefs,scenarios}.py`, `cli/agent_stack.py`; `cli.py` 624 → 368 lines |
| **6. Daemon bootstrap + consent** | ✅ Complete | `core/policy.py` (moved from daemon), `daemon/consent.py` (`ConsentEvaluator`+`ConsentDecision`), `daemon/bootstrap.py` (`build_daemon` → `DaemonSubsystems`); `daemon/main.py` 484 → 261 lines; heartbeat-inject duplication fixed |
| 7. Polish | ⏳ Not started | Typed channel/stream payloads, `TriageAgent` docstring, `TaskRunnerAgent` constructor cleanup, silent-except fix, `Loop` Protocol |

---

## Step 1 — What was completed

### New files (storage scaffolding)
- [yuyutsava/storage/__init__.py](yuyutsava/storage/__init__.py) — package boundary doc
- [yuyutsava/storage/paths.py](yuyutsava/storage/paths.py) — `state_dir`, `sessions_db_path`, `state_db_path`, `checkpoints_db_path`, `interrupts_db_path`, `blobs_dir`, `events_config_path`
- [yuyutsava/storage/ids.py](yuyutsava/storage/ids.py) — `mint_thread_id`, `parse_thread_id_ts` (single source for the `<role>-<unix_ts>-<uuid4>` format the sweeper parses)
- [yuyutsava/storage/base.py](yuyutsava/storage/base.py) — `BaseSqliteStore` with shared WAL/busy_timeout/write-lock/migration. **No store inherits from it yet** — Step 2 swaps them over.
- [yuyutsava/storage/models.py](yuyutsava/storage/models.py) — placeholder; Step 2 populates with typed records

### Modified files

**Path functions removed from `core/config.py`, callers updated to `storage.paths`:**
- [yuyutsava/core/config.py](yuyutsava/core/config.py) — removed `yuyutsava_home`, `sessions_db_path`, `interrupts_db_path`, `events_config_path`; re-imports `events_config_path` from storage.paths for internal use
- [yuyutsava/mcp/config.py](yuyutsava/mcp/config.py) — inline import killed, uses `state_dir()`
- [yuyutsava/sessions/config.py](yuyutsava/sessions/config.py) — imports from `storage.paths`
- [yuyutsava/cli/cli.py](yuyutsava/cli/cli.py) — dead `yuyutsava_home` import removed
- [yuyutsava/daemon/db_introspect.py](yuyutsava/daemon/db_introspect.py) — uses `state_db_path()`
- [yuyutsava/daemon/permissions_policy.py](yuyutsava/daemon/permissions_policy.py) — inline import pulled up, uses `state_dir()`
- [yuyutsava/daemon/main.py](yuyutsava/daemon/main.py) — uses `state_dir`, `blobs_dir`, `checkpoints_db_path`
- [yuyutsava/events/store.py](yuyutsava/events/store.py) — uses `state_db_path()`

**Duplicated `thread_id` minting killed:**
- [yuyutsava/sessions/sqlite_store.py](yuyutsava/sessions/sqlite_store.py) — removed `mint_thread_id` definition; imports from `storage.ids`
- [yuyutsava/daemon/checkpointing.py](yuyutsava/daemon/checkpointing.py) — removed `thread_id` and `_parse_ts` definitions; uses `parse_thread_id_ts` from `storage.ids`
- [yuyutsava/daemon/orchestrator_loop.py](yuyutsava/daemon/orchestrator_loop.py) — imports `mint_thread_id` from `storage.ids`
- [yuyutsava/sessions/__init__.py](yuyutsava/sessions/__init__.py) — re-exports `mint_thread_id` from `storage.ids`
- [test/sessions/test_sqlite_store.py](test/sessions/test_sqlite_store.py) — imports `mint_thread_id` from `storage.ids`

**`LimitsConfig` + `TimingConfig` added to `core/config.py`:**
- New module-level `LIMITS` and `TIMING` dataclass singletons exposed via `from yuyutsava.core.config import LIMITS, TIMING`
- Fields: `max_tool_result_chars`, `max_stdout_chars`, `max_prefs_chars`, `max_skill_index_chars`, `max_skill_desc_chars`, `docker_max_output_bytes`, `sqlite_busy_timeout_ms`, `tool_default_timeout_sec`, `bash_default_timeout_sec`
- Wired into [core/engine.py](yuyutsava/core/engine.py), [prefs/injector.py](yuyutsava/prefs/injector.py), [core/docker_sandbox_backend.py](yuyutsava/core/docker_sandbox_backend.py), [skills/registry.py](yuyutsava/skills/registry.py)
- Dead constant `_MAX_STDOUT_CHARS` removed from `core/engine.py` (was defined but never referenced)

**Inline imports pulled up:**
- [core/permission_middleware.py](yuyutsava/core/permission_middleware.py) — `os`
- [daemon/voice_channel.py](yuyutsava/daemon/voice_channel.py) — `re`
- [events/store.py](yuyutsava/events/store.py) — `fnmatch`
- [prefs/injector.py](yuyutsava/prefs/injector.py) — fake-circular dep switched to `TYPE_CHECKING`; dropped runtime `isinstance` assert
- [sessions/sqlite_store.py](yuyutsava/sessions/sqlite_store.py) — `SessionsSettings`
- [cli/cli.py](yuyutsava/cli/cli.py) — `shlex`, `time`, `json` (the `json as _json` alias was removed; `_json.X` → `json.X`)

**Inline imports deliberately kept** (justified):
- `core/engine.py` daemon imports (real circular-dep avoidance)
- `events/registry.py` source loading (explicit lazy)
- `events/bus.py` optional `ulid` dep
- `agents/task_runner/permissions.py` agent_context cycle
- `cli/cli.py:_prefs_main` imports of `events.store.Store` and `prefs.store.UserPrefsStore` — these stay inline until Step 5 CLI extraction so the CLI cold path doesn't pull the daemon's store at startup

### Verification done
- All 22 touched modules import cleanly (`uv run python -c "import …"`)
- Full test suite passes (`uv run python -m unittest discover -s test -v` → 13 ran, 0 failed)
- CLI runs: `uv run yuyutsava --help` and `uv run yuyutsava --list-sessions` work against existing DBs (path migration is byte-equivalent for default locations)

### Known gotchas for the next chat
- [core/interrupts_store.py:9](yuyutsava/core/interrupts_store.py#L9) has a docstring comment pointing to `yuyutsava.core.config.interrupts_db_path` — the function moved to `storage.paths`. **Cosmetic only**, will be fixed when this file moves to `storage/interrupts.py` in Step 2.
- `events/store.py` keeps using `yuyutsava_home()` indirectly via its constructor path. The state.db path now resolves through `state_db_path()`. No behavior change.
- The `_DEFAULT_STORE` lazy singleton in `sessions/sqlite_store.py` is **still process-wide**. Tests must reset it between runs (already-known issue, separate from this restructure).

---

## Step 2 — What was completed

### New files
- [yuyutsava/storage/sessions/__init__.py](yuyutsava/storage/sessions/__init__.py), [store.py](yuyutsava/storage/sessions/store.py), [sqlite_impl.py](yuyutsava/storage/sessions/sqlite_impl.py), [checkpointer.py](yuyutsava/storage/sessions/checkpointer.py), [config.py](yuyutsava/storage/sessions/config.py)
- [yuyutsava/storage/interrupts.py](yuyutsava/storage/interrupts.py) — `InterruptsStore` inheriting `BaseSqliteStore`, typed `record(InterruptRecord)` API
- [yuyutsava/storage/events/__init__.py](yuyutsava/storage/events/__init__.py), [store.py](yuyutsava/storage/events/store.py)
- [yuyutsava/storage/prefs.py](yuyutsava/storage/prefs.py) — `PrefsStore` (renamed from `UserPrefsStore`)

### Models populated ([storage/models.py](yuyutsava/storage/models.py))
- `Session`, `SESSION_STATUSES` — moved from sessions/models.py
- `Proposal`, `ConsentRule` — moved from events/store.py (now frozen, with class methods)
- `EventRecord`, `Decision`, `Pref` — **new**, replace `dict[str, Any]` returns
- `InterruptRecord` with `from_payload()` classmethod — **new**, replaces loose dict on the interrupts API

### Stores now inherit `BaseSqliteStore`
- `SqliteSessionStore` and `InterruptsStore` use the shared WAL+busy_timeout+write-lock+migration pattern from `storage/base.py`. Each defines its own `_SCHEMA_VERSION`, `_SCHEMA_SQL`, and `_META_TABLE`. Duplicated retry/migration code is now killed in those two stores.
- The events `Store` keeps its long-lived sync connection + writer-queue design (different access pattern from session/interrupt stores — see "Deferred" below).

### Typed DAO returns
- `Store.get_event_payload()` → `EventRecord | None`
- `Store.get_proposal()` → `Proposal | None`
- `Store.list_consent_rules()` → `list[ConsentRule]`
- `Store.list_decisions()` → `list[Decision]`
- `InterruptsStore.list_for_session()` / `list_recent()` → `list[InterruptRecord]`
- `Store.recall()` deliberately stays `list[dict[str, Any]]` — it's a projected join over multiple tables, used only by the orchestrator's `recall` tool to render JSON for the LLM; building dataclasses here just to discard them is wasted work.

### Callers updated (18 sites, clean break, no shims)

**`from yuyutsava.events.store import …` → `from yuyutsava.storage.events import …`** (Store, Proposal, ConsentRule):
- agents/face_watcher/agent.py, agents/file_organizer/agent.py, agents/orchestrator/agent.py, agents/orchestrator/spawn.py
- daemon/main.py, daemon/triage_loop.py, daemon/orchestrator_loop.py, daemon/blob_sweeper.py, daemon/events_sweeper.py
- daemon/channels.py, daemon/voice_channel.py, daemon/terminal_channel.py
- daemon/web/services/stream_service.py
- events/tools.py, events/registry.py, events/source.py
- cli/cli.py

**`from yuyutsava.prefs.store import UserPrefsStore` → `from yuyutsava.storage.prefs import PrefsStore`**:
- prefs/injector.py (also switched to `TYPE_CHECKING`)
- cli/cli.py, daemon/main.py, daemon/web/routers/logs.py

**`from yuyutsava.core.interrupts_store import InterruptsStore` → `from yuyutsava.storage.interrupts import InterruptsStore`**:
- sessions/runner.py
- core/engine.py — also tightened `interrupts_store: "Any | None"` → `interrupts_store: InterruptsStore | None` on both `_prompt_permission` and `astream_agent`; calls now build an `InterruptRecord.from_payload(...)` before persisting

**Sessions storage import path:**
- The `sessions/` package now only exports `run_session` and `ResumeFailed`. Storage types (`SessionStore`, `SqliteSessionStore`, `SessionsSettings`, `build_checkpointer`, `get_default_session_store`, `SessionNotFound`) come from `yuyutsava.storage.sessions`. `Session` comes from `yuyutsava.storage.models`.
- cli/cli.py, daemon/web/routers/sessions.py, daemon/web/schemas/session.py, test/sessions/test_sqlite_store.py, test/sessions/test_runner_crash.py — all updated.

### Dict-access bugs fixed downstream of typed reads
- `daemon/triage_loop.py:_match_rule` — was using `rule["topic_glob"]`, `rule.get("expires_ts")`, etc. Now uses dataclass attribute access (`rule.topic_glob`, `rule.expires_ts`). Return type changed to `ConsentRule | None`.
- `daemon/triage_loop.py:_handle` — `rule["decision"]` / `rule["rule_id"]` → `rule.decision` / `rule.rule_id`. Dead defensive call `rule.get("subagent_hint")` (column never existed) removed — falls back to `"file-organizer"` directly.
- `events/tools.py:fetch_event` — `json.dumps(rec, ...)` where `rec` was a dict; now explicitly projects the `EventRecord` fields into a dict for JSON serialization.
- `daemon/web/routers/decisions.py`, `daemon/web/routers/rules.py` — wrap return rows in `dataclasses.asdict(...)` so FastAPI serializes typed dataclasses to JSON without changing the HTTP contract.

### Dead code removed
- `Store.expire_proposals()` — never called (no callers in repo, audit confirmed).

### Deleted files
- `yuyutsava/sessions/store.py`, `sqlite_store.py`, `checkpointer.py`, `config.py`, `models.py`
- `yuyutsava/core/interrupts_store.py`
- `yuyutsava/events/store.py`
- `yuyutsava/prefs/store.py`

### Inline imports also cleaned (deferred from Step 1)
- `cli/cli.py:_prefs_main` — `Store` and `PrefsStore` imports moved to top of file (now lighter; the storage layer doesn't drag in daemon stack)
- `storage/sessions/sqlite_impl.py:create` — `mint_thread_id` moved to top-of-file

### Deferred to a later step (documented in `storage/events/__init__.py`)
- **Per-table class split**: the plan envisioned splitting events/store.py into `EventStore` / `ProposalStore` / `ConsentRuleStore` / `QuotaStore` sharing a `StateDb` owner. Deferred because (a) all 18 callers receive `Store` and use 1–6 different tables — splitting would force each call site to take 1–5 store params; (b) the current single-writer queue is intentional (one async task serializing writes from many sources) and is harder to preserve cleanly across N stores with N write locks. The single typed `Store` class with typed reads delivers ~80% of the readability win without the caller churn. Revisit if a caller emerges that genuinely uses only one table.
- **Interrupts store docstring** at `core/interrupts_store.py:9` was deleted along with the file, so the broken docstring link from Step 1 is naturally resolved.

### Verification done
- All 22+ touched modules import cleanly
- All 13 existing tests still pass (sessions sqlite store + crash recovery)
- `uv run yuyutsava --list-sessions` works end-to-end against the same `~/.yuyutsava/sessions.db` (no data migration needed, same schema)

### Known gotchas for the next chat
- `_DEFAULT_STORE` lazy singleton in `storage/sessions/sqlite_impl.py` is still process-wide (carry-over from before — known issue, unchanged here).
- `storage/events/__init__.py` documents the per-table-split deferral. If that bothers anyone in Step 3+, the typed records are already in `storage/models.py` so a future split won't require re-typing.
- `storage/prefs.py` is a thin typed wrapper that still holds a `Store` reference. Once `Store` is split per-table (deferred), `PrefsStore` should own its own `BaseSqliteStore` connection. For now they share.

---

## Step 3 — What was completed

### New files
- [yuyutsava/storage/introspect.py](yuyutsava/storage/introspect.py) — moved from `daemon/db_introspect.py`. `list_databases()` now returns `list[DatabaseInfo]` (frozen dataclass), not `list[dict]`. The `DatabaseInfo` dataclass is co-located here — it's exclusively used by introspect, no need to bloat `storage/models.py`.
- [yuyutsava/storage/sweeper.py](yuyutsava/storage/sweeper.py) — new `UnifiedSweeper` consolidating checkpoint + blob + events TTL sweeps behind one loop, one `SweeperConfig`, one `SweepReport`. Implements `async def run(stop_event)` so it slots into the daemon's main task set alongside `TriageLoop` / `OrchestratorLoop`.
  - `BlobSweepTarget` moved here (was in `daemon/blob_sweeper.py`); per-target TTL preserved so different blob dirs can age at different rates.
  - One log line per tick showing typed counter report (`checkpoints=N blob_files=N blob_rows=N event_rows=N`).

### Modified files

**`daemon/checkpointing.py` — trimmed to lifecycle only.** The `CheckpointerManager` class is gone; the slim `CheckpointerSaver` class owns just the `AsyncSqliteSaver` async-context. The TTL sweep half moved to `UnifiedSweeper`. The saver lifecycle stays in daemon because opening the saver requires an `AsyncExitStack` and the daemon's main entry is the natural place to hold that.

**`daemon/main.py` — three sweepers → one.**
- Imports: dropped `BlobSweeper`, `BlobSweepTarget` from `daemon.blob_sweeper`, `EventsSweeper` from `daemon.events_sweeper`, `CheckpointerManager` from `daemon.checkpointing`.
- New imports: `CheckpointerSaver` from `daemon.checkpointing`; `BlobSweepTarget`, `SweeperConfig`, `UnifiedSweeper` from `storage.sweeper`.
- Construction: `checkpointer_saver = CheckpointerSaver(...)` → `checkpointer = await checkpointer_saver.start()`; one `sweeper = UnifiedSweeper(store=..., checkpoint_saver=checkpointer, blob_targets=[BlobSweepTarget("webcam", ...)], config=SweeperConfig())`.
- Task wiring: added `asyncio.create_task(sweeper.run(stop_event), name="unified-sweeper")` to the concurrent loops list. The sweeper is joined via the same `asyncio.gather` cleanup path the other loops use; no separate `await sweeper.stop()` needed.
- Shutdown: replaced three separate `await ...stop()` calls with one `await checkpointer_saver.stop()` after the gather.

**`daemon/web/routers/db.py`** — import path `daemon.db_introspect` → `storage.introspect`. `list_databases()` now returns dataclasses, so `get_databases` projects via `dataclasses.asdict(d)` into the existing Pydantic `DatabaseInfo` schema. HTTP contract unchanged.

**`agents/db_tools/tools.py`** — same import-path update; `db_list` agent tool now does `[asdict(d) for d in await list_databases()]` before JSON-serializing.

**`events/sources/webcam.py`** — docstring reference updated `daemon.blob_sweeper.BlobSweeper` → `storage.sweeper.UnifiedSweeper`.

**`storage/__init__.py`** — removed the "(Step 3)" tag from `introspect` / `sweeper` lines now that they're populated.

### Deleted files
- `yuyutsava/daemon/db_introspect.py` (→ `storage/introspect.py`)
- `yuyutsava/daemon/blob_sweeper.py` (folded into `storage/sweeper.py`)
- `yuyutsava/daemon/events_sweeper.py` (folded into `storage/sweeper.py`)

### Design notes / deferrals
- **Per-target blob TTL preserved.** The plan's `SweeperConfig.blob_ttl_sec` field would have flattened all blob targets to one TTL; kept per-target instead because future sources (audio clips, screenshot bursts) want different windows on the same loop.
- **Single loop interval.** Defaults to 5 min; the events sweeper used to run on a 1-hour cadence but the DELETE is a no-op once the table is caught up so the extra ticks are cheap. One knob (`sweep_interval_sec`) replaces three.
- **Logging on every tick.** Counters are 0 when there's nothing to delete; that's a stable health signal for the daemon without spamming three different log lines.
- **`CheckpointerSaver` stays in daemon.** Plan §3.7 noted "consider keeping a slim CheckpointerSaver lifecycle owner in daemon" — done. Storage doesn't know about `AsyncExitStack`; daemon's main loop owns the lifecycle.
- **Introspect constants left at module level.** `DEFAULT_LIMIT`, `MAX_LIMIT`, `QUERY_TIMEOUT_SEC` stayed in `storage/introspect.py` rather than absorbed into `core/config.LimitsConfig`/`IntrospectConfig`. The plan §7.3 mentions `IntrospectConfig` but that's a Step 7 polish concern; moving them now would be a second concern in this PR. Easy follow-up later.

### Verification done
- `uv run python -c "import yuyutsava.storage.introspect, yuyutsava.storage.sweeper; print('ok')"` ✅
- `uv run python -c "import yuyutsava.daemon.main, yuyutsava.daemon.checkpointing, yuyutsava.daemon.web.routers.db, yuyutsava.agents.db_tools.tools; print('ok')"` ✅
- `uv run python -m unittest discover -s test -v` — 13 tests, 0 failures ✅
- Direct introspect smoke test against the live `~/.yuyutsava/state.db`:
  - `list_databases()` returned typed `DatabaseInfo` rows for `state` + `sessions`.
  - `list_tables('state')` returned the seven existing tables.
  - `execute_read_query('state', 'SELECT COUNT(*) AS n FROM event_payloads')` returned `[{'n': 171}]`.
- `uv run yuyutsava --list-sessions` works end-to-end (3 sessions listed against existing DB).

### Known gotchas for the next chat
- `storage/sweeper.py` imports `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` directly. Pure logic stays clean but a future Postgres backend would need to lift this through a `Checkpointer` Protocol. Defer until needed.
- `UnifiedSweeper` logs at `INFO` on every tick. If that becomes noisy in production, gate by `report.total > 0` or lower to `DEBUG`. Currently a feature, not a bug — gives a stable signal that the unified sweeper is alive.
- Introspect's `list_tables` / `table_schema` still return `list[dict[str, Any]]`. They're free-form by design (column metadata is shaped by SQLite's PRAGMA) — typing them is low ROI and the HTTP layer already validates via Pydantic at the boundary. Left as-is intentionally.

---

## Step 5 — What was completed

### New files
- [yuyutsava/cli/agent_stack.py](yuyutsava/cli/agent_stack.py) — `build_cli_agent_stack(workspace, settings, *, …) -> AgentBundle`. Single place that constructs `SkillRegistry` + `TaskRunnerAgent` + `GeneralPurposeAgent` + the compiled deepagent. Reusable from tests or any future second entry point — no copy-paste of 30 lines of wiring.
- [yuyutsava/cli/commands/__init__.py](yuyutsava/cli/commands/__init__.py) — package boundary doc, no exports.
- [yuyutsava/cli/commands/chat.py](yuyutsava/cli/commands/chat.py) — `run_chat(…)` owns the main task flow: open checkpointer, build agent stack, drive `run_session`, optional docker pull, sandbox cleanup, final-print formatting.
- [yuyutsava/cli/commands/prefs.py](yuyutsava/cli/commands/prefs.py) — `run_prefs(argv)` (was `cli.py:_prefs_main`). Procedural — argparse dispatches to it.
- [yuyutsava/cli/commands/sessions.py](yuyutsava/cli/commands/sessions.py) — `print_sessions_table(workspace_filter)` + `delete_session(id)` (was `_print_sessions_table` + `_delete_session_cmd`). Carries the ANSI/age/bytes helpers since nothing else uses them.
- [yuyutsava/cli/commands/scenarios.py](yuyutsava/cli/commands/scenarios.py) — moved verbatim from `cli/scenarios.py`. Same `Scenario` dataclass, `get_scenario`, `format_scenario_list`.

### Modified files
- [yuyutsava/cli/cli.py](yuyutsava/cli/cli.py) — 624 → 368 lines. Now strictly: `_build_parser()` + `main(argv)` dispatch + `_async_main(argv)` shortcircuit handlers + the 3 settings-from-args helpers (`_resolved_execution_mode`, `_docker_settings_from_args`, `_local_settings_from_args`). The chat flow is one `await run_chat(…)` call. The plan target was ~250 but ~200 of the remaining lines is argparse declarative help-text — pure declarative parser config has nowhere else to live and shouldn't be split per-flag.

### Deleted files
- `yuyutsava/cli/scenarios.py` (→ `cli/commands/scenarios.py`)

### Design notes / deferrals
- **CLI stays procedural.** Confirmed scope decision held: no `CliApp` class. `main()` → dispatch → command function. The new command modules each export plain functions (`run_chat`, `run_prefs`, `print_sessions_table`, `delete_session`).
- **No second entry point built.** `agent_stack.py` is the seam — if a `yuyutsava-eval` or similar binary appears later, it imports `build_cli_agent_stack` instead of re-wiring. Not pre-building that binary.
- **The 3 settings-from-args helpers stay in `cli.py`.** They each translate `argparse.Namespace` into one config dataclass; lifting them into the commands package would force a second hop for what's essentially "parse my flags." `cli.py` is the right home — it's the place that owns `args`.
- **`run_chat` takes keyword-only args (12 of them).** Could have packed them into a `ChatOptions` dataclass; intentionally didn't. The call site in `cli.py` is one place, the args are flat, and the dataclass would just duplicate `argparse.Namespace` fields for zero readability win. Revisit if a second caller emerges that wants to skip argparse.

### Verification done
- `uv run python -c "import yuyutsava.cli.cli, yuyutsava.cli.agent_stack, yuyutsava.cli.commands.chat, yuyutsava.cli.commands.prefs, yuyutsava.cli.commands.scenarios, yuyutsava.cli.commands.sessions; print('ok')"` ✅
- `uv run python -m unittest discover -s test -v` — 13 tests, 0 failures ✅
- `uv run yuyutsava --help` — full parser output renders ✅
- `uv run yuyutsava --list-scenarios` — shows all 4 scenarios (`get_scenario` + `format_scenario_list` reach through `commands/scenarios.py`) ✅
- `uv run yuyutsava --list-sessions` — short-circuits into `print_sessions_table`, renders the 3 existing sessions ✅
- `uv run yuyutsava prefs list` — dispatches to `run_prefs`, prints the 2 existing prefs (`daemon.log_level`, `interaction.style`) ✅

### Known gotchas for the next chat
- `cli/commands/chat.py` imports `_cleanup_local_sandbox` from `core/engine` — that's a private name. It's the only "private import" boundary in the new layout. Either rename it public in `engine.py` (`cleanup_local_sandbox`) when Step 7 touches engine again, or leave it — only the CLI owns the local sandbox so a one-caller private function is defensible.
- `agent_stack.py` does not register MCP tools or any other future subagent type. When new subagent classes are added to the CLI, this is the single function to edit.
- `run_chat` reads `os` indirectly through `_resolved_execution_mode` in `cli.py` (env var fallback), but doesn't import `os` itself. If a second non-CLI caller wants the same env-var fallback, it should pass `execution_mode` explicitly — that's the contract.

---

## Step 6 — What was completed

### New files
- [yuyutsava/core/policy.py](yuyutsava/core/policy.py) — moved verbatim from `daemon/permissions_policy.py`. `PermissionsPolicy`, `StorePolicyCapEnforcer`, `PolicyEntry`, `today_utc` now live in `core/` so the CLI permission middleware can consume the same model. Untyped-`store` comment updated to point at `storage.events.Store`.
- [yuyutsava/daemon/consent.py](yuyutsava/daemon/consent.py) — `ConsentEvaluator` + frozen `ConsentDecision(rule: ConsentRule | None)`. Encapsulates Tier-1 consent-rule lookup with the same `topic_glob` + `match_json` predicate semantics as before. `ConsentDecision.matched` is a `@property` shortcut for `rule is not None`.
- [yuyutsava/daemon/bootstrap.py](yuyutsava/daemon/bootstrap.py) — `DaemonOptions` (frozen, CLI-args record: `workspace`, `headless`, `voice`, `verbose`), `DaemonSubsystems` (frozen, ~15 fields covering every wired subsystem), and one `async def build_daemon(opts) -> DaemonSubsystems`. Boot order preserved: configs → store → prefs → policy → MCP → checkpointer → sweeper → bus → sources → channels → models → skills → search → subagents → triage → loops → web server.

### Modified files

**`daemon/main.py` — 484 → 261 lines (lifecycle only).**
- Removed every subsystem import; now imports only `DaemonOptions, DaemonSubsystems, build_daemon` from bootstrap and `MCPConfig` for the SIGHUP reload path.
- Flow is exactly: argparse → `_setup_logging` → `DaemonOptions(...)` → `await build_daemon(opts)` → re-apply log level from persisted prefs → install signal handlers → `_log_ready_banner(subs)` → schedule loop tasks → wait → ordered teardown.
- The `_reload_loop(subs, stop, reload)` now reaches `subs.mcp_manager.hot_reload(...)` and `subs.hot_reload_events_config()` (the latter is a closure returned by bootstrap that captures `registry` + `daemon_cfg`).
- Cleanup path unchanged: stop sources → close bus → drain loops → shutdown channels → stop MCP → close checkpointer-saver → stop store.

**`daemon/triage_loop.py` — Tier-1 consent extraction.**
- `_match_rule` and `_match_predicate` deleted (~40 lines).
- Dead imports removed: `fnmatch`, `from typing import Any`.
- New import `from yuyutsava.daemon.consent import ConsentEvaluator`.
- `__init__` constructs `self._consent = ConsentEvaluator(store)`; `_handle()` calls `rule = self._consent.evaluate(ev).rule` and branches on the returned `ConsentRule | None` exactly like before. Loop semantics unchanged.

**`daemon/bootstrap.py` — heartbeat duplication fixed.**
- The two-place injection at the old `main.py:163-181` + `main.py:357-366` collapsed into one `_inject_heartbeat(events_cfg, heartbeat_sec)` helper. Called once at boot (via `_build_initial_events_config`) and once inside the hot-reload closure. Same observable behaviour, one place to change.

**`agents/face_watcher/agent.py`** — docstring reference `permissions_policy.py` → `core/policy.py`. Cosmetic.

### Deleted files
- `yuyutsava/daemon/permissions_policy.py` (→ `core/policy.py`)

### Design notes / deferrals
- **`DaemonOptions` separate from `DaemonConfig`.** Plan §6.1 sketched `build_daemon(config: DaemonConfig)` but `DaemonConfig` is env-derived (`from_env()`) and doesn't know about CLI flags like `--voice` or `--no-ui`. Splitting cleanly: `DaemonOptions` carries args, `DaemonConfig` carries env. Bootstrap reads `DaemonConfig.from_env()` itself; `main.py` doesn't need to know.
- **`hot_reload_events_config` is a closure returned by bootstrap, not a method on `DaemonSubsystems`.** Returning a method would force the closure variables (`registry`, `daemon_cfg`) onto the dataclass as fields. A `Callable[[], Awaitable[None]]` field hides the binding and keeps `DaemonSubsystems` flat.
- **Web server stays inside bootstrap.** It depends on `web_hub`, `daemon_cfg.web_host/port`, `skill_registry`, and the reload closure — all built in bootstrap. Moving it back to `main.py` would force re-passing four fields. The uvicorn `_run_uvicorn` driver itself stays in `main.py` because it's lifecycle (`stop_event`-bound).
- **Electron auto-launch stays in main.py.** It's a side effect tied to "the daemon is ready and the user is interactive" — that's a lifecycle decision, not a wiring step. Bootstrap returns `web_url` so main.py can launch electron without recomputing it.
- **`render_capabilities_block` was already an unused import in `triage_loop.py`.** Left untouched — pre-existing, not part of Step 6's scope. Step 7 can sweep it.
- **`StorePolicyCapEnforcer.store: object` stays untyped.** The cycle is policy → store → events.store → (no longer) policy, but typing it would still drag the `Store` import into `core/`. The `# type: ignore[attr-defined]` line works fine. Revisit if `core/` ever genuinely needs to import storage.

### Verification done
- `uv run python -c "import yuyutsava.daemon.main, yuyutsava.daemon.bootstrap, yuyutsava.daemon.consent, yuyutsava.core.policy; print('ok')"` ✅
- `uv run python -m unittest discover -s test -v` — 13 tests, 0 failures ✅
- `grep -rn "daemon\.permissions_policy" yuyutsava test` — 0 hits anywhere in the repo ✅
- `uv run yuyutsava --help` / `--list-sessions` / `daemon --help` all render correctly ✅
- Functional smoke test of `ConsentEvaluator` against a fake store:
  - Matching event (`topic=fs.changed`, `hints.ext=pdf`, rule glob `fs.*` + predicate `hints.ext=pdf`) → `matched=True`, `rule.decision="auto_approve"` ✅
  - Non-matching event (same topic, `hints.ext=jpg`) → `matched=False` ✅

### Known gotchas for the next chat
- `daemon/bootstrap.py` is 392 lines, much longer than `daemon/main.py` (261). That's expected — the wiring volume didn't shrink, it just moved. If a future change makes bootstrap noticeably bigger, split per-subsystem (`_build_storage`, `_build_loops`, etc.) inside the same file — don't create new packages just to spread call sites.
- `DaemonSubsystems` includes a few fields used only for boot logging (`triage_settings`, `orchestrator_settings`, `subagent_names`). They're cheap, they're frozen, and they keep main.py's banner code from reaching back into bootstrap internals. If you find another consumer wanting to reach into bootstrap state, prefer adding a field here over exposing internals.
- `TriageLoop` still imports `render_capabilities_block` (unused). Pre-existing dead import; Step 7 polish.

---

## Next step — Step 7: Polish

### Goal
Close out the remaining type-safety and quality items from plan §7. This is the smallest step — five surgical changes, each independent.

### Concrete changes

| From | To |
|---|---|
| [daemon/channels.py:53,64](yuyutsava/daemon/channels.py#L53) `ChannelEvent.data: dict[str, Any]`, `AskPrompt.interrupt_value: dict[str, Any]` | typed `ChannelPayload` (Pydantic discriminated union or frozen dataclass) |
| [daemon/web/services/stream_service.py:30,43](yuyutsava/daemon/web/services/stream_service.py#L30) `dict[str, Any]` stream items | `StreamEvent` Pydantic model with `kind: Literal[...]` discriminator |
| [agents/triage/agent.py:54](yuyutsava/agents/triage/agent.py#L54) `class TriageAgent:` (no docstring explaining non-`BaseSubAgent` status) | 3-line class docstring per plan §7.7 |
| [agents/task_runner/agent.py:64-76](yuyutsava/agents/task_runner/agent.py#L64) dual `sandbox_subdir`/`sandbox_root` API | drop `sandbox_subdir`, keep `sandbox_root: Path \| None = None`; callers compute the path |
| [sessions/runner.py:50-90](yuyutsava/sessions/runner.py#L50) `_patch_orphan_cancellations` silent `except Exception: return 0` + `type(m).__name__ == "ToolMessage"` | add `logger.exception(...)` before the return; replace with `isinstance(m, ToolMessage)` |

### Other Step-7 work
- **`Loop` Protocol.** New `daemon/loops.py` with one `@runtime_checkable Protocol`:
  ```python
  class Loop(Protocol):
      async def run(self, stop_event: asyncio.Event) -> None: ...
  ```
  `TriageLoop`, `OrchestratorLoop`, `UnifiedSweeper` already conform — no inheritance needed. Future loops just satisfy the signature.
- **Sweep `cli/commands/chat.py`'s `_cleanup_local_sandbox` private import** from `core/engine` (the one "private import" boundary called out in Step 5's known gotchas). Either rename it public or document the boundary with a comment.
- **Sweep the dead `render_capabilities_block` import** from `daemon/triage_loop.py` (carry-over from before Step 6).
- **`TaskRunnerAgent` constructor cleanup will ripple.** Callers in `daemon/bootstrap.py` and `cli/agent_stack.py` pass `workspace_root=workspace` and rely on the default `sandbox_subdir="_sandbox"`. After the change they keep working (default `sandbox_root=None` resolves to `workspace_root / "_sandbox"`). The `policy` arg also stays. Audit the two call sites to confirm no one was passing `sandbox_subdir` by keyword.

### Verification recipe for Step 7
1. `uv run python -c "import yuyutsava.daemon.channels, yuyutsava.daemon.web.services.stream_service, yuyutsava.daemon.loops, yuyutsava.agents.triage.agent, yuyutsava.agents.task_runner.agent, yuyutsava.sessions.runner; print('ok')"`
2. `uv run python -m unittest discover -s test -v` — 13 tests still pass.
3. `uv run python -c "from yuyutsava.daemon.loops import Loop; from yuyutsava.daemon.triage_loop import TriageLoop; from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop; from yuyutsava.storage.sweeper import UnifiedSweeper; assert isinstance.__call__ is not None"` — Protocol introspection sanity.
4. `grep -rn 'dict\[str, Any\]' yuyutsava/daemon/channels.py yuyutsava/daemon/web/services/stream_service.py` — zero hits for payload fields (other `Any` usage on metadata bags is OK).
5. `uv run yuyutsava-daemon` boot + send one `fs.changed` event → consent path → proposal lands in `state.db::proposals` → web `/proposals` returns typed payload (Pydantic schema, not free-form dict).
6. CLI smoke: `uv run yuyutsava` chat → permission prompt → approve → completion. Session row + interrupts row + checkpoint thread all written.

### Honest pushback to remember
- Plan §11 already said: **don't aim for 100% Pydantic.** Only attack store reads, channel payloads, stream events, interrupts. Don't type LangGraph's `configurable={"thread_id": ...}` dicts — that's their API.
- Don't add inheritance to satisfy the `Loop` Protocol. `runtime_checkable` means `isinstance(loop, Loop)` Just Works; no `class TriageLoop(Loop):` needed.

---

## How to resume in a new chat

Paste this prompt:

```
Read $REPO/RESTRUCTURE_HANDOFF.md and
$HOME/.claude/plans/i-want-you-to-nifty-starlight.md to get full
context on the YUYUTSAVA restructure work. Steps 1-6 are complete and
verified. Begin Step 7 (Polish: typed channel/stream payloads, Loop Protocol,
TriageAgent docstring, TaskRunnerAgent constructor cleanup, silent-except
fix) per the plan. Keep the user-confirmed decisions: clean-break migration,
full restructure, CLI stays procedural.
```

A fresh chat reading both docs will have:
- The full architectural review (plan)
- Exactly what was done in Steps 1-6 (this handoff)
- What Step 7 entails with file:line targets
- The verification recipe to confirm Step 7 lands without regressions
