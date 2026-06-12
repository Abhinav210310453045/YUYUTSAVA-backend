# Implementation Progress — MASTER_PLAN.md

> Protocol: READ this file at the start of every session. UPDATE it at the end of every
> work block (check off items, note deviations from `docs/MASTER_PLAN.md` and WHY).
> One phase at a time, in order. Branch per phase: `feature/phase-<N>-<slug>` off `yuyutsava-daemon`.

## Prompt for a fresh session

> Read docs/MASTER_PLAN.md and docs/IMPLEMENTATION_PROGRESS.md in this repo, then
> continue implementing the next unfinished phase exactly as specified. Follow the
> conventions in MASTER_PLAN §4 and update IMPLEMENTATION_PROGRESS.md when done.

## Session log

| Date | Session | Work done |
|---|---|---|
| 2026-06-12 | 1 | Plan finalized → docs/MASTER_PLAN.md. Branch `feature/phase-1-postgres-context`. **Phase 1 implemented end-to-end** (1A–1D + tests + PG integration verification). |
| 2026-06-12 | 2 | **Phase 2 implemented end-to-end** (auth, task submission/registry, tasks API, per-task SSE + ring replay, tests + live-PG verification of migration v2). Per user instruction, work stayed on `feature/phase-1-postgres-context` (no new phase branch). |

## Phase 1 — Postgres backend + Context Controller  [CODE COMPLETE — 2 manual checks open]

### 1A Postgres plumbing
- [x] deps installed: langgraph-checkpoint-postgres **3.1.0** (compatible with checkpoint 4.1.1 — plan risk #3 cleared), psycopg[binary,pool] 3.3.4; extra `memory`: pgvector 0.4.2
- [x] `yuyutsava/storage/backend.py` (StorageSettings.from_env; default DSN `postgresql://yuyutsava:yuyutsava@127.0.0.1:5433/yuyutsava`)
- [x] `yuyutsava/storage/pg/pool.py` (PgPool, autocommit pool, open(wait=True))
- [x] `yuyutsava/storage/pg/migrations.py` (pg_advisory_lock, schema_meta anchor, v1: artifacts + thread_summaries + memories + HNSW index)
- [x] `docker-compose.postgres.yml` (pgvector/pgvector:pg16, host port **5433**)
- [x] `storage/sessions/checkpointer.py` postgres branch (AsyncPostgresSaver)
- [x] `daemon/checkpointing.py` CheckpointerSaver postgres branch + `fallback_reason` + YUYUTSAVA_STORAGE_REQUIRE fail-fast
- [x] `daemon/bootstrap.py` storage block (pool → migrations → stores; loud TimelinePayload on fallback after channels exist)
- [x] `storage/sweeper.py` `_enumerate_thread_ids` dispatch (sqlite vs postgres `_cursor()`)
- [x] DaemonConfig budget from_env fallbacks fixed: orchestrator 8000→60000, subagent 30000→60000 (both now match dataclass defaults)

### 1B Context controller (`yuyutsava/context/`)
- [x] config.py — ContextSettings (role-aware via `_env`; provider-derived max_input_tokens map)
- [x] artifacts.py — ArtifactStore ABC + SqliteArtifactStore (state.db, own `artifacts_meta`) + PgArtifactStore; shared grep
- [x] tools.py — ctx_fetch_artifact / ctx_grep_artifact (plain-text bracket-header responses)
- [x] offload_middleware.py — ToolResultOffloadMiddleware via awrap_tool_call; JSON digest {offloaded, artifact_id, head 1500, tail 500, hint}; storage failure → passthrough (guard_tool_result remains display backstop)
- [x] summary_store.py — ThreadSummaryStore ABC + Sqlite/Pg twins, versioned per thread
- [x] compaction.py — YuyutsavaCompactionMiddleware(SummarizationMiddleware): pinning (leading Human/System only), persistence, memory embedding, abefore_agent resume injection, 6-section structured prompt
- [x] injector.py — MemoryInjector (mirrors PrefsInjector); `LimitsConfig.max_memory_chars=2000` added
- [x] Sweeper: artifact TTL sweep via `artifact_store` param + `SweeperConfig.artifact_ttl_sec` (7d)

### 1C Semantic memory (`yuyutsava/memory/`)
- [x] config.py (YUYUTSAVA_MEMORY_ENABLED, EMBED_* role env, YUYUTSAVA_EMBED_MODEL=nomic-embed-text, dim 768) / embedder.py (httpx OpenAI-compatible) / store.py (PgMemoryStore cosine + NULL-embedding degradation; SqliteMemoryStore keyword twin) / tools.py (mem_search, mem_save)
- [x] write paths: compaction summaries (memory_sink), task outcomes in OrchestratorLoop._run_task, mem_save tool
- [x] read paths: MemoryInjector at task start (RELEVANT MEMORY block merged with prefs block), mem_search; orchestrator prompt updated (TOOLS list + RULE 1)
- [x] `mem_` added to ToolFilterMiddleware._SUPPRESS_PREFIXES; `ctx_` deliberately always-visible

### 1D Wiring
- [x] engine.py `_context_middleware()` helper; build_orchestrator master middleware = [ToolFilter, Offload, Compaction, Budget]; subagent specs get fresh ctx middleware + ctx_* tools appended to spec["tools"]
- [x] engine.py build_cli_deepagent: new optional params (artifact/summary/memory stores, context_settings, compaction_model); ctx+mem tools ride `extra_tools` into the ToolRegistry
- [x] bootstrap.py: storage block, stores, MemorySettings (default-on when PG live), compaction model (role `compaction`), ContextSettings (role `orchestrator`), OrchestratorDeps new fields, OrchestratorLoop memory_injector, DaemonSubsystems {pg_pool, artifact_store, summary_store, memory_store, embedder}
- [x] daemon/main.py teardown: embedder.aclose → pg_pool.close (after checkpointer/store stop)
- [x] cli/agent_stack.py: SQLite stores + ContextSettings(role "cli") + compaction model wired always-on

### Tests (all green; `uv run python -m unittest discover -s test`)
- [x] test/context/test_artifacts.py — roundtrip, slicing, grep+lineno, invalid regex, TTL delete
- [x] test/context/test_offload_middleware.py — 150k→<3k digest + retrievable, small/excluded/non-ToolMessage/store-failure passthrough
- [x] test/context/test_compaction.py — trigger math, under-threshold no-op, pinning, **parallel tool-call pairs never split**, summary persisted with all sections, memory sink, resume injection, **3-cycle continuity (## SESSION INTENT survives, summary v3)**
- [x] test/context/test_summary_store.py, test/memory/test_store.py

### Postgres integration verification (live pgvector:pg16 container, 2026-06-12)
- [x] migrations apply + idempotent re-apply; tables {artifacts, thread_summaries, memories, schema_meta} + HNSW index
- [x] Pg artifact/summary stores roundtrip; memory add/search degrades gracefully without embed endpoint
- [x] AsyncPostgresSaver checkpoint put/get/delete via build_checkpointer
- [x] dead-PG → loud SQLite fallback (`fallback_reason` set); REQUIRE=1 → boot refusal
- [x] CLI deepagent graph builds with compaction nodes + ctx tools in compiled graph

### Definition of Done — remaining MANUAL checks (need user's LLM keys / desktop)
- [ ] Daemon boot with `YUYUTSAVA_STORAGE_BACKEND=postgres`, run a real task, `kill -9`, restart, confirm thread resumes from PG
- [ ] Long CLI session with repeated ws_* searches → langfuse shows input-token plateau (not linear growth)

### Deviations from MASTER_PLAN (all intentional)
1. **Compose at repo root** (`docker-compose.postgres.yml`, not `infra/`) — matches existing `docker-compose.langfuse.yml` convention. Host port **5433** to avoid clashing with any system Postgres.
2. **Compaction trigger uses `("tokens", N)`** computed from ContextSettings, not `("fraction", f)` — langchain's fraction form requires model-profile data Groq/Ollama/OpenRouter don't ship (raises ValueError in `SummarizationMiddleware.__init__`).
3. **Artifact sweep** = `artifact_store` param + `SweeperConfig.artifact_ttl_sec` on UnifiedSweeper (cleaner than a file-based `ArtifactSweepTarget`; artifacts live in DB, not on disk).
4. **CLI uses SQLite twins** for artifacts/summaries/memory even when `YUYUTSAVA_STORAGE_BACKEND=postgres` (no pool lifecycle owner in the CLI yet); CLI **checkpoints** do honor postgres via build_checkpointer. Known gap: daemon-on-postgres doesn't sweep CLI sqlite artifacts — revisit in Phase 2 when TaskRegistry reuses the PG plumbing.
5. **Summary prompt has 6 sections** (added ## ARTIFACTS to the plan's 5) so offloaded artifact ids survive compaction.
6. Pre-existing, unrelated: `test/test_async.py` fails at import with a live-LLM 401 (invalid GROQ key in env) — fails identically without Phase 1 changes.

## Phase 2 — Gateway hardening  [CODE COMPLETE — 2 manual checks open]

### New files
- [x] `yuyutsava/daemon/web/auth.py` — `AuthSettings.from_env(host=)`; bearer middleware (constant-time compare); `/health` public; `?token=` accepted ONLY on `/stream`; token auto-generated to `~/.yuyutsava/api_token` (0600) on non-loopback bind when env unset; pure `check_request()` for testability
- [x] `yuyutsava/daemon/task_registry.py` — `TaskRecord`, `TaskStore` ABC + `SqliteTaskStore` (state.db, own `tasks_meta`) / `PgTaskStore` twins, `TaskRegistry` (queued→running→done|failed|cancelled, in-memory cancel-request set, cursor pagination via ULID-ordered `tsk_` ids)
- [x] `yuyutsava/daemon/task_submission.py` — `submit_direct` (auto-approved Proposal + decision audit row + immediate enqueue; `session_hint` → proposal.session_id for origin-aware ask routing) / `submit_via_triage` (publishes `user.task.submitted` on the bus; task_id rides `EventEnvelope.hints`)
- [x] `yuyutsava/daemon/web/routers/tasks.py` + `web/schemas/task.py` — `POST /tasks {instruction, mode}`, `GET /tasks?status=&limit=&cursor=`, `GET /tasks/{id}`, `POST /tasks/{id}/cancel` (coarse v1), `GET /tasks/{id}/events` (ring replay)

### Modified
- [x] `web/app.py` — loopback refusal replaced: non-loopback allowed **iff** auth token present (else RuntimeError); auth middleware installed before CORS (preflights bypass the 401); CORS origins from `YUYUTSAVA_CORS_ORIGINS` (fallback: legacy loopback regex); tasks router wired; http-log middleware documented as path-only (no query → no token leak)
- [x] `daemon/orchestrator_loop.py` — `task_registry` param; `_register_task` (mints rows for organic event-born tasks: origin `event:<topic>`); mark_running w/ thread_id; cancel pre-check (queued) + between-stream-events check; mark_done w/ result_summary; mark_failed + re-raise; all emitted ChannelEvents tagged task_id/session_id
- [x] `daemon/channels.py` — `ChannelEvent` gains optional `task_id`/`session_id`
- [x] `web/services/stream_service.py` — `StreamEventItem` carries + serializes the tags; `WebHub` per-task ring (500 items/task, 64 tasks max, oldest evicted) + `task_events()`; `WebChannel` forwards tags (also `cli_remote_channel.py`)
- [x] `web/routers/stream.py` — `?task_id=` / `?session_id=` filters in the SSE responder (hub unchanged); `item_matches()` exported for tests
- [x] `daemon/triage_loop.py` — `OrchestratorTask.task_id` field (default ""); both enqueue paths carry `ev.hints["task_id"]`
- [x] `storage/pg/migrations.py` — v2: `tasks` table + status index
- [x] `daemon/bootstrap.py` — task store (PG/SQLite by backend) → `TaskRegistry` → `TaskSubmissionService` (after task_queue); `AuthSettings.from_env(host)`; make_app forwards auth/registry/submission; uvicorn `access_log` disabled when auth enforced (query strings carry `?token=`); `DaemonSubsystems` gains task_registry/task_submission (no teardown — stores ride pg_pool / per-call sqlite)

### Tests (all green; full suite 97 tests, only pre-existing `test_async` 401 import error remains)
- [x] test/daemon/test_task_registry.py — roundtrip, transitions, cancel ok/not_found/conflict, pagination + status filter, column allowlist
- [x] test/daemon/test_task_submission.py — direct: registry row + queue join + approved proposal + decision; triage: bus envelope w/ hints task_id, nothing enqueued; empty-instruction rejection
- [x] test/daemon/test_triage_task_id.py — hints task_id → OrchestratorTask; organic events stay ""
- [x] test/daemon/test_orchestrator_loop_registry.py — fake-graph: queued→running→done + thread_id + tagging; organic row minting; cancel between events; cancel-before-start; failure → failed + re-raise
- [x] test/web/test_auth.py — 401/200 non-loopback, loopback token-free, `/health` public, query-token only on /stream, refuse non-loopback w/o token
- [x] test/web/test_tasks_api.py — submit/list/detail/cancel/replay over httpx ASGI; **e2e: POST /tasks → orchestrator (fake stream) → GET shows done + result_summary + replayable events**
- [x] test/web/test_stream_filter.py — item_matches matrix; ring bounded per task; untagged not ringed; oldest-task eviction

### Postgres integration verification (live pgvector:pg16 container, 2026-06-12)
- [x] migration v2 applies + idempotent re-apply (`schema_meta` → 2; `tasks` table present)
- [x] PgTaskStore full lifecycle roundtrip; `list(status=)` + cancel-on-terminal → conflict; `created_ts` returns as float (twin parity)

### Definition of Done — remaining MANUAL checks (need user's tailnet / LLM keys)
- [ ] Bind `YUYUTSAVA_DAEMON_HOST=100.x.y.z` (tailnet), hit from a second device with the bearer token
- [ ] mode=triage with a real triage LLM: proposal appears in Electron, approve → task runs (code path covered by unit tests up to the bus)

### Deviations from MASTER_PLAN (all intentional)
1. **No `feature/phase-2-*` branch** — user instructed to keep working on `feature/phase-1-postgres-context`.
2. **PG `tasks` timestamps are `DOUBLE PRECISION` epoch seconds**, not TIMESTAMPTZ — the registry reads them back and serves them over the API as the same floats the SQLite twin stores (byte-identical wire schema across backends).
3. **Triage-mode submissions get a registry row at submit time** (plan only required orchestrator transitions); join key flows `hints["task_id"]` → OrchestratorTask. A triage task the user skips / triage drops stays `queued` forever — coarse v1, revisit if it bothers.
4. **Organic event-born tasks also get registry rows** (minted in OrchestratorLoop, origin `event:<topic>`) so `GET /tasks` shows everything the orchestrator runs, not just API submissions.
5. **`submit_via_triage` returns the task_id** (plan left the return unspecified) so `POST /tasks` always answers `{task_id}` regardless of mode.
6. **uvicorn access_log off when auth is enforced** — uvicorn logs full request lines incl. query strings (`?token=` on /stream); the plan only mentioned `_broadcast_http_log`, which was already path-only.
7. **WebHub rings capped at 64 tracked tasks** (oldest evicted) on top of the plan's 500-items-per-task, so an immortal daemon can't grow rings unboundedly.
8. `decision_service` extraction from `routers/proposals.py` NOT done here — the plan schedules it for Phase 3 (it's the InboundSink seam).
## Phase 3 — Channel plugins + Telegram  [NOT STARTED — next up]
## Phase 4 — Model routing + cost  [NOT STARTED]
## Phase 5 — Resource governor  [NOT STARTED]
## Phase 6 — Mobile API contract  [NOT STARTED]
