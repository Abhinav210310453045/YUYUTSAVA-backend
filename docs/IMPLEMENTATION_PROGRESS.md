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
| 2026-06-12 | 3 | **Phase 3 implemented end-to-end** (decision_service extraction, ChannelRouter register/unregister, `yuyutsava/channels/` plugin framework + ChannelPluginRegistry, Telegram reference plugin, /channels API, origin-aware ask routing). Same branch. |
| 2026-06-12 | 4 | **Phase 4 implemented end-to-end** (ModelRouter + tier env roles, ComplexityScorer, triage complexity scoring, llm_usage table + UsageRecorder middleware, GET /usage, tasks.model column, tests + live-PG verification of migration v3). Same branch. |
| 2026-06-12 | 5 | **Phase 5 implemented end-to-end** (psutil dep, ResourceMonitor + AdmissionController in daemon/resources.py, admission.slot() wrapping OrchestratorLoop._run_task, deferred_ms recording, SystemMetricsPayload, GET /system/metrics, tests). Same branch. |

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
## Phase 3 — Channel plugins + Telegram  [CODE COMPLETE — real-bot manual checks open]

### New files
- [x] `yuyutsava/daemon/web/services/decision_service.py` — `DecisionService` extracted from `routers/proposals.py` (deferred from Phase 2 per plan §Phase 3): flips proposal status + resolves the blocking `asyncio.Future` wherever it lives. Surfaces register their pending maps via `add_waiters` (WebHub at boot, InboundSink for plugins); `pending_ids()` feeds `InboundSink.list_pending`. Raises `DecisionConflictError`; router maps it to 409
- [x] `yuyutsava/channels/plugin.py` — `ChannelPlugin(UserChannel)` ABC (`plugin_id`, `capabilities` {notify,proposal,ask,invoke}, `start(sink)/stop()`, `from_config(params)`) + `InboundSink` facade (`submit_task`→submit_direct, `respond_proposal/respond_ask`→DecisionService, `list_pending` (registry queued/running + pending ids), `daemon_status`, `get_state/put_state` (PrefsStore), and `pending_proposals/pending_asks` maps in the WebHub-future pattern)
- [x] `yuyutsava/channels/config.py` — `ChannelsConfig`/`ChannelConfig`, `~/.yuyutsava/channels_config.json` (`channels_config_path()` added to storage/paths.py, env override `YUYUTSAVA_CHANNELS_CONFIG`), EventsConfig-shaped json, atomic `to_file`, `with_enabled`
- [x] `yuyutsava/channels/registry.py` — `ChannelPluginRegistry` modeled on SourceRegistry: `start_all` (one bad plugin logs, never kills boot), `enable`/`disable` idempotent under an asyncio lock (single instance per name → never two pollers per bot token; router name-collision rolls back with `plugin.stop()`), coarse `reload`, `snapshot()` for GET /channels; static factory map v1 `{"telegram": …}` (lazy import)
- [x] `yuyutsava/channels/telegram/client.py` — minimal httpx Bot API client (no python-telegram-bot): getUpdates/sendMessage/editMessageText/answerCallbackQuery/setMyCommands/getMe; exp backoff on network errors 1s→60s (6 attempts), honors 429 `retry_after`; token never logged
- [x] `yuyutsava/channels/telegram/channel.py` — `TelegramChannelPlugin`: env `YUYUTSAVA_TELEGRAM_BOT_TOKEN` (env-only) + `YUYUTSAVA_TELEGRAM_CHAT_IDS` allowlist (non-allowlisted dropped + WARNING). Outbound: Token/HttpLog suppressed, Log/Timeline debounced 2s, completion classes (`event-action`/`event-error`, AsyncTaskCompleted) flush immediately as background sends; post_proposal = inline keyboard [Approve][Skip][Modify…] + future in sink map, honors expires_ts, edits outcome into the message; post_ask = options keyboard or force-reply free text. Inbound: callback_query → sink.respond_*; modify → force-reply follow-up; `/tasks`, `/status`, `/help`; plain text → `sink.submit_task(origin="telegram")`; offset persisted in user_prefs key `telegram.offset` after each batch
- [x] `yuyutsava/daemon/web/routers/channels.py` — `GET /channels` (snapshot), `POST /channels/{name}/enable|disable` (persists config + hot-applies; 404 unknown, 422 misconfigured e.g. missing token)

### Modified
- [x] `daemon/channels.py` — `ChannelRouter.register()` (idempotent by name) / `unregister(name)` / `find(name)`; `routers/cli_attach.py` refactored onto them (formalizes the hand-mutation precedent)
- [x] `routers/proposals.py` — thin veneer over the shared DecisionService (`Depends(get_decision_service)`)
- [x] `web/app.py`/`server.py`/`deps.py` — forward + expose `decision_service` and `channel_plugins`; create_app builds a hub-local DecisionService when none passed (tests/embedded unchanged); channels router wired
- [x] `daemon/bootstrap.py` — `SessionOriginMap` now ALWAYS constructed (was async-subagents-gated; it's a plain dict, no langgraph import); DecisionService(store) + hub waiters; InboundSink (status_provider closure); `ChannelPluginRegistry` built after task_submission, `start_all()` before loops; `DaemonSubsystems` gains `decision_service` + `channel_plugins`
- [x] `daemon/orchestrator_loop.py` — `_map_session_origin`: when a task's registry-row origin equals a registered channel name (e.g. "telegram"), map the run's `thread_id` → that channel in `session_origin` so Tier-2 asks route back to the submitting surface; cleared in `finally`
- [x] `daemon/main.py` — teardown: `channel_plugins.stop_all()` before `channels.shutdown()`

### Tests (full suite 145 tests; only the pre-existing `test_async` 401 import error remains)
- [x] test/channels/test_registry.py — FakeChannelPlugin lifecycle: enable→router fan-out; disable→removed+stopped+no fan-out; double-enable/disable idempotent (single instance); reload applies config diff (fresh instance, new params); unknown plugin KeyError; router-collision rollback; start_all survives a broken factory; snapshot shape. + ChannelRouter register/unregister idempotence
- [x] test/channels/test_config.py — default/roundtrip/with_enabled/invalid-json
- [x] test/web/test_decision_service.py — resolution across multiple waiter maps; modify carries edited_instruction (non-modify drops it); conflict; invalid decision; no-listener note; ask blank→"reject"; pending_ids; duplicate add_waiters no-op; **HTTP regression**: /proposal/{id}/respond + /ask/{id}/respond unchanged post-extraction (200/409, hub future resolved)
- [x] test/channels/telegram/test_channel.py — token/http-log suppression; 2s debounce batching; completion immediate flush; proposal approve via button (keyboard layout, message edit, callback answered); modify flow via force-reply; expiry → "expired"; ask options keyboard + free-text force-reply; plain text → submit_task("telegram"); non-allowlisted drop; /status, /tasks; **offset persisted and fresh instance resumes from it**
- [x] test/web/test_channels_api.py — list/enable/disable over httpx ASGI incl. config-file persistence, idempotent re-enable, 404/422
- [x] test/daemon/test_orchestrator_session_origin.py — channel-origin mapped, api-origin not, None-map and blank-task safe

### Definition of Done — remaining MANUAL checks (need user's real bot)
- [ ] Real test bot: task completion notification arrives; proposal approved via button; "summarize ~/Downloads" from phone → task runs → completion message back
- [ ] Daemon restart → poller resumes from persisted offset against live Bot API (covered by unit test with fake client)

### Deviations from MASTER_PLAN (all intentional)
1. **DecisionService owns a list of waiter maps** instead of only the WebHub futures — the plan's sequence routes Telegram callbacks through the sink to one shared implementation, but the blocking future for a Telegram-shown proposal lives in the plugin, not WebHub. Surfaces register their maps (`add_waiters`); plugins park futures in `InboundSink.pending_proposals/asks`.
2. **InboundSink gained `get_state`/`put_state`** (PrefsStore-backed) beyond the plan's five methods — the Telegram offset must persist in `user_prefs` and the sink is the only daemon surface a plugin sees.
3. **`channels_config.json` lives under `~/.yuyutsava/`** (per plan) with a `YUYUTSAVA_CHANNELS_CONFIG` env override for tests, unlike repo-local events_config.json — which channels a user enabled is runtime state, not a project artifact.
4. **Origin-aware ask routing implemented in OrchestratorLoop** (`_map_session_origin`), not in the plugin: ask `session_id` is the per-task `thread_id` minted at run start, which the plugin never sees. The loop maps thread→origin-channel when the registry row's origin matches a registered channel name. `SessionOriginMap` is now always constructed (it was async-subagents-gated; it's a thread-safe dict with no heavy imports).
5. **DecisionConflictError re-exported via `yuyutsava.channels.plugin`** so plugins don't import daemon web internals.
6. **`/channels/{name}/enable` returns 422 (not 500) on misconfiguration** (missing bot token etc.) and does NOT persist `enabled: true` for a plugin that failed to start.
7. **Telegram outbound goes to every allowlisted chat** (plan didn't specify a primary); first answer wins for proposals/asks.
## Phase 4 — Model routing + cost  [CODE COMPLETE — 1 manual check open]

### New files
- [x] `yuyutsava/core/model_router.py` — `ModelTier`, `RoutingSettings.from_env()` (`YUYUTSAVA_MODEL_ROUTING`, `YUYUTSAVA_ROUTING_THRESHOLDS="2,3"`), `ModelRouter` (tier_for / lazy+cached `tier_model` via `llm_settings_from_env("tier_light|standard|heavy")` / `model_for(complexity, fallback=)` — flag OFF or misconfigured tier → fallback, never blocks); `PRICES` (USD per 1M in/out, prefix-keyed, longest prefix wins) + `load_price_table()` (`~/.yuyutsava/model_prices.json` override) + `estimate_cost_usd()`; `ComplexityScorer` (lazy model factory, one-digit prompt, fallback 3 on ANY failure)
- [x] `yuyutsava/daemon/usage.py` — `UsageContext` (task_id/thread_id join keys), `UsageRow`/`UsageAggregate`, `UsageStore` ABC + `SqliteUsageStore` (state.db, own `llm_usage_meta`) / `PgUsageStore` twins (`list`, `aggregate(since, group_by=task|model|day|None)` — shared SQL builder, day via strftime/to_char), `UsageRecorder(AgentMiddleware)` (`aafter_model`, input+output tokens from `usage_metadata`, est cost; no-usage calls skipped; store failure logged + swallowed)
- [x] `yuyutsava/daemon/web/routers/usage.py` + `web/schemas/usage.py` — `GET /usage?since=&group_by=` (422 on bad group_by, 503 when store unwired)

### Modified
- [x] `agents/triage/agent.py` — `TriageDecision.complexity: int = 3` (ge=1 le=5); `agents/triage/prompts.py` — anchored-examples paragraph (move one file=1 … refactor code across files=5)
- [x] `daemon/triage_loop.py` — `OrchestratorTask.complexity: int = 3`; LLM path carries `decision.complexity` through `_handle_user_decision(…, complexity=)`; auto-approve rule path scores 1 (no LLM in the loop; it IS the anchored complexity-1 example)
- [x] `daemon/task_submission.py` — `submit_direct(complexity=None)`: client override (clamped 1–5) wins, else one light-tier scoring call when a scorer is wired; unscored stays NULL on the row, OrchestratorTask defaults 3. `TaskSubmitIn` gains optional `complexity`
- [x] `daemon/orchestrator_loop.py` — `model_router` param; `_select_models()` per task (router absent/disabled → booted role models, byte-identical); routed subagent model rides a `dataclasses.replace` per-task deps copy; `mark_running(complexity=, model=)`; `UsageContext` passed to `build_orchestrator`
- [x] `daemon/task_registry.py` — `TaskRecord.model` column; SQLite `_SCHEMA_VERSION` 1→2 (idempotent `ALTER TABLE tasks ADD COLUMN model` in `_migrate`); `create(complexity=)`; `mark_running(complexity=, model=)`
- [x] `core/engine.py` — `build_orchestrator(usage_context=)`; `_usage_mw()` appends `UsageRecorder` to master middleware (after budget — passive accounting) and to every subagent spec, keyed by `deps.usage_store` (None → [], pre-Phase-4 identical)
- [x] `core/llm.py` — `model_name_of()` helper (model_name | model attr)
- [x] `storage/pg/migrations.py` — v3: `llm_usage` table + ts/task indexes + `ALTER TABLE tasks ADD COLUMN IF NOT EXISTS model`
- [x] `daemon/bootstrap.py` — usage store (PG/SQLite by backend), `ModelRouter.from_env()`, scorer only when routing enabled (a score nobody routes on is wasted spend), threaded into TaskSubmissionService / OrchestratorDeps / OrchestratorLoop / make_app; `DaemonSubsystems` gains `usage_store` + `model_router` (no teardown — rides pg_pool / per-call sqlite)
- [x] `web/app.py` + `server.py` + `web/deps.py` — `usage_store` app-state + `get_usage_store` + usage router wired

### Tests (full suite 196 tests; only the pre-existing `test_async` 401 import error remains)
- [x] test/core/test_model_router.py — threshold parsing (default/custom/malformed), tier mapping + None/out-of-range clamp, **flag-off passthrough returns fallback identity**, lazy build + per-tier cache (ollama tier, no keys needed), misconfigured-tier fallback, longest-prefix pricing, known-token cost sums, price-file merge + malformed-file tolerance, scorer (digit / prose digit / garbage→3 / LLM failure→3 / factory failure→3 / model resolved once)
- [x] test/daemon/test_usage.py — store roundtrip, list filters, **aggregate totals/task/model/day with known token counts (cost rows sum correctly)**, order by cost, bad group_by; recorder writes row w/ cost + join keys, one row per call, no-usage/no-messages skipped, store failure swallowed
- [x] test/daemon/test_triage_complexity.py — TriageDecision default+bounds, user-decision path carries complexity, auto-approve path = 1
- [x] test/daemon/test_task_submission.py (extended) — scorer used when no override, client override wins (scorer not consulted), override clamped, no-scorer → NULL row + task default 3
- [x] test/daemon/test_orchestrator_routing.py — complexity-1 → light model in build_orchestrator (+ per-task deps copy, booted deps untouched), complexity-5 → heavy, disabled router → role models + same deps object, no-router pre-Phase-4 behaviour, registry row records complexity + model name, UsageContext join keys match thread_id
- [x] test/daemon/test_task_registry.py (extended) — mark_running records complexity+model; **v1 state.db migrates in place** (ALTER adds model, legacy rows readable)
- [x] test/web/test_usage_api.py — totals/model/task groupings over httpx ASGI, cost-ordered, 422 bad group_by, 503 missing store

### Postgres integration verification (live pgvector:pg16 container, 2026-06-12)
- [x] migration v3 applies + idempotent re-apply (`schema_meta` → 3; `llm_usage` cols + `tasks.model` present)
- [x] PgUsageStore add/list + all four aggregate shapes (None/task/model/day); PgTaskStore row carries complexity + model through mark_running (twin parity; verification rows cleaned up)
- [x] `build_orchestrator` compiles with `usage_store` + `UsageContext` wired (smoke)

### Definition of Done — remaining MANUAL check (needs user's Ollama/LLM keys)
- [ ] Integration with two real models configured as light/standard (e.g. Ollama): submit complexity-1 task with `YUYUTSAVA_MODEL_ROUTING=1` → assert `llm_usage.model` shows the light model (unit-level equivalent covered by test_orchestrator_routing)

### Deviations from MASTER_PLAN (all intentional)
1. **`tasks` gains a `model` column** (PG v3 + SQLite tasks_meta v2): the plan says "record complexity + chosen model in TaskRegistry" but the Phase-2 schema had no model column — added rather than abusing result_summary.
2. **Scorer only constructed when routing is enabled** — a complexity score nobody routes on is a wasted LLM call; rows from unscored direct tasks keep `complexity NULL` ("never scored") while the OrchestratorTask defaults to 3.
3. **Auto-approve (consent-rule) path scores complexity 1**, not the default 3 — there is no LLM in that path and a rule-approved single-file move is literally the prompt's anchored complexity-1 example.
4. **`UsageRecorder` model name is fixed at construction** (the routed model), falling back to `response_metadata.model_name` — deterministic attribution to the model the router chose, which is what the audit join needs.
5. **`ComplexityScorer` parses a digit from a plain completion** instead of structured output — light-tier models (tiny Ollama) are unreliable with tool-calling/structured output; regex `[1-5]` + fallback 3 is more robust.
6. **Misconfigured tier falls back to the role model** (logged) instead of raising — routing must never make a runnable task unrunnable; the plan only specified flag-off behaviour.
7. **`GET /usage` without `group_by` returns one `key="all"` totals row** (plan left ungrouped behaviour unspecified).
## Phase 5 — Resource governor  [CODE COMPLETE — 1 manual check open]

### New files
- [x] `yuyutsava/daemon/resources.py` — `ResourceSettings.from_env()` (`YUYUTSAVA_RES_CPU_HIGH_PCT=85`, `YUYUTSAVA_RES_MEM_MIN_MB=1024`, `YUYUTSAVA_RES_DISK_MIN_GB=5`, `YUYUTSAVA_MAX_HEAVY_TASKS=1`, `YUYUTSAVA_RES_SAMPLE_SEC=5`, `YUYUTSAVA_RES_DEFER_MAX_SEC=600`, plus `YUYUTSAVA_RES_EMIT_SEC=10`, `YUYUTSAVA_RES_HEAVY_COMPLEXITY=4`, `YUYUTSAVA_RES_HEAVY_HINTS=` csv, `YUYUTSAVA_RES_DOCKER_STATS=0`); `ResourceSnapshot` (cpu_pct / mem_available_mb / disk_free_gb / per_container / ts); `ResourceMonitor` (psutil sampler, ring of 120 samples, `snapshot()`/`ring()`/`loaded()`/`disk_critical()`, lifecycle `run(stop_event)` à la UnifiedSweeper, debounced `SystemMetricsPayload` emission while tasks run); `AdmissionController` (`weight_for` = complexity ≥ 4 or hint in heavy set; `slot(task, task_id=)` async ctx manager: heavy → disk check → `Semaphore(max_heavy)` → load deferral w/ 2s→30s backoff capped at defer_max then **run anyway**; `DiskCriticalError` fails the task; `active()` per-task attribution; deferred_ms → `TaskRegistry.set_deferred_ms`)
- [x] `yuyutsava/daemon/web/routers/system.py` + `web/schemas/system.py` — `GET /system/metrics` (current snapshot + ring + loaded/disk_critical + heavy_slots {max,in_use} + active_tasks attribution; 503 when monitor unwired; admission absent → monitor-only output)

### Modified
- [x] `daemon/channels.py` — `SystemMetricsPayload` (kind `system_metrics`) added to the `ChannelPayload` union (serializes through the existing generic `StreamEventItem.to_wire_dict`)
- [x] `daemon/orchestrator_loop.py` — `admission` param; `_run_task` body wrapped in `async with admission.slot(task, task_id=)` (null context when unwired — pre-Phase-5 identical); slot entered **before** `mark_running` so deferral happens while the row still reads `queued`; cancel re-check after the slot (a task cancelled during a long deferral never starts the graph); `_cancel_before_start` helper extracted (used by both cancel paths); `DiskCriticalError` propagates through the existing failure path → `mark_failed` with the clear disk message
- [x] `daemon/bootstrap.py` — `ResourceSettings.from_env()` → `ResourceMonitor` (event_sink=channels.post_event) → `AdmissionController` (registry + event_sink) after the channels block; `monitor.activity_probe = lambda: bool(admission.active())`; threaded into OrchestratorLoop + make_app; `DaemonSubsystems` gains `resource_monitor` + `admission` (no teardown — the monitor loop joins on stop_event like the sweeper)
- [x] `daemon/main.py` — `resource-monitor` task scheduled alongside triage/orchestrator/sweeper loops
- [x] `web/app.py` + `server.py` + `web/deps.py` — `resource_monitor`/`admission_controller` app-state, `get_resource_monitor` (503) / `get_admission_controller` (None-degrade), system router wired
- [x] `pyproject.toml` — `psutil>=5.9` (installed 7.2.2)

### Tests (full suite 218 tests; only the pre-existing `test_async` 401 import error remains)
- [x] test/daemon/test_resources.py — settings env parsing (defaults / overrides / hint csv / malformed fallback), monitor ring + loaded/disk_critical flags (scripted sampler), live psutil sample sanity, SystemMetricsPayload debounce + activity gating; admission: weight matrix incl. heavy-hint set, **light passes immediately on a loaded system**, **heavy defers w/ backoff then runs once load clears (deferred_ms=6000 recorded)**, **deferral ceiling → runs anyway**, **Semaphore(1) caps concurrent heavies** (+ "waiting for slot" event), **disk-critical raises DiskCriticalError without leaking the semaphore**, unloaded heavy passes with no deferral row
- [x] test/daemon/test_orchestrator_admission.py — fake-graph loop integration: heavy task deferred then done w/ `deferred_ms` on the registry row + "deferred — system busy" timeline event; disk-critical → status `failed` w/ clear error, graph never runs; **cancel during deferral → cancelled, graph never runs, slot released**; light task unaffected by load
- [x] test/web/test_system_api.py — empty-ring shape, samples + active heavy task attribution over httpx ASGI, admission-absent degradation, 503 missing monitor

### Definition of Done
- [x] Unit: admission math with fake snapshot provider — loaded system: heavy defers then runs, light passes immediately; semaphore caps concurrent heavies; disk-critical fails task
- [ ] Integration (manual, needs a real busy machine + LLM keys): busy-loop the machine, submit complexity-5 task → observe `deferred:` timeline event then execution; `deferred_ms` recorded (unit-level equivalent covered by test_orchestrator_admission)
- [x] `GET /system/metrics` returns sane values on macOS (live psutil smoke: cpu 12%, mem 3879MB free, disk 243GB free; + unit sanity test)
- [x] Progress file updated

### Deviations from MASTER_PLAN (all intentional)
1. **Docker container stats are opt-in** (`YUYUTSAVA_RES_DOCKER_STATS=1`, default off) — sampling forks a `docker stats` CLI subprocess every tick; the snapshot schema carries `per_container` either way and failures degrade to `{}`. The Docker per-task hard cage (DockerSettings) is untouched.
2. **Heavy-hint set defaults empty** (`YUYUTSAVA_RES_HEAVY_HINTS` csv) — Phase 4 shipped, so complexity is the primary weight signal; no subagent hint is inherently heavy. The complexity threshold is also env-tunable (`YUYUTSAVA_RES_HEAVY_COMPLEXITY`, default 4 per plan).
3. **Cancel re-checked after admission deferral** (not in plan) — a heavy task the user cancels during a 10-minute deferral is marked cancelled and never starts the graph.
4. **Disk-critical gates only heavy tasks** (matches the plan's flow diagram — light tasks bypass the gate entirely).
5. **deferred_ms includes semaphore wait** (time queued behind another heavy task), not just load deferral — it measures "how long admission held the task back", which is what the column documents; ~0ms holds are not written.
6. **Admission slot wraps `mark_running`** so a deferred task stays `queued` in `GET /tasks` until it actually starts (plan didn't pin the ordering).
7. **`clock`/`sleep` injectable on AdmissionController** — deterministic deferral/backoff tests without real waiting.

## Phase 6 — Mobile API contract  [NOT STARTED]
