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

## Phase 2 — Gateway hardening  [NOT STARTED — next up]
## Phase 3 — Channel plugins + Telegram  [NOT STARTED]
## Phase 4 — Model routing + cost  [NOT STARTED]
## Phase 5 — Resource governor  [NOT STARTED]
## Phase 6 — Mobile API contract  [NOT STARTED]
