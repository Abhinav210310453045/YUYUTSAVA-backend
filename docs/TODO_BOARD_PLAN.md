# TODO Board + TinkerAgent — Master Plan

## Context

YUYUTSAVA gets a new flagship feature: a **global TODO board** that acts as the user's day-to-day planning and *thinking* surface — mimicking the pen-and-paper problem-solving process. Each TODO is a rich card (renamable title, multiple notes, attachments: files/images/videos/links/diagrams/artifacts), not a one-liner. A **dedicated TinkerAgent** — deliberately separate from the master orchestrator — helps the user tinker: refine vague ideas into sharper ones (improve the input, don't blabber a whole answer), break goals into small objectives, generate diagrams/artifacts, research, and ask active HITL questions. Accessible from UI, CLI, chat and voice.

**User-locked design decisions:**
- Phased build (like the voice feature): plan whole, implement phase-by-phase with review pauses.
- TinkerAgent = **dedicated deepagent bundle** (new engine factory), living by yuyutsava rules: ToolRegistry + `tool_search` lazy discovery, ToolFilterMiddleware, TaskRunner `bind_tools` gateway, consent/HITL, context-management middleware (offload → compaction → transcript RAG), per-agent skills. It may use existing sync/async subagents (no spawn-subagent tool).
- **One thread per TODO card** (`thread_id = "todo:<card_id>"`), leaning on existing context management incl. semantic transcript recall so the agent never bloats.
- **Per-card workspace** for tr_* tools, plus queryable metadata so any master agent can learn about TODOs.
- **Common exchange protocol**: a versioned schema through which the user, master, CLI, REST, and TinkerAgent all write/read the board — no raw-table access by other agents. First-class exception handling.
- **Pluggable artifact blocks**: adding a new artifact kind (txt/md/html now; JSX sandbox, audio later) must require no core edits. Loose coupling everywhere.
- MCP: generic user-configured MCP servers available to TinkerAgent; all existing design tools (vis_*, Kroki diagrams, code-as-image) available through `tool_search`.
- Master orchestrator can capture/list TODOs via lightweight tools; later phase registers TinkerAgent as an **async subagent** of the master, with asks/help routed to the UI (ChannelRouter plumbing already exists).

## Architecture

```
                        ┌────────────── Electron UI ──────────────┐
                        │ TodosPanel (board) ── Card expanded view │
                        │   notes/STT · attachments · artifacts    │
                        │   "Think with TinkerAgent" (chat/voice)  │
                        └───────┬───────────────────┬─────────────┘
                          REST /v1/todos      WS /ws/converse?agent=tinker
                                │                   │
   CLI / master chat ──todo_* tools──►  ┌───────────▼───────────┐
   ("add this as a TODO")               │  todoboard.exchange   │  ◄── the ONLY write/read path
                                        │  (versioned schemas,  │
                                        │   typed exceptions)   │
                                        └───────────┬───────────┘
                                        TodoStore (Routed: Pg ⇄ Sqlite buffer)
                                        blobs: blobs_dir()/todoboard/<card_id>/
```

New package **`yuyutsava/todoboard/`**: `models.py` (exchange schemas), `exchange.py` (protocol + exceptions), `store.py` (Pg/Sqlite/Routed), `tools.py` (`todo_*` family), `artifacts.py` (artifact-block registry). Agent in **`yuyutsava/agents/tinker/`** (`agent.py`, `prompts.py`). Skills in **`yuyutsava/skills/bundled/tinker/`**.

### 1. Data layer (pattern: `message_feedback` + `visuals`)
- **PG migration v16** appended to `MIGRATIONS` in `yuyutsava/storage/pg/migrations.py`:
  - `todo_cards` (`card_id` `tdo_`+ULID, `title`, `status` CHECK (`inbox/active/done/archived`), `pinned`, `tags jsonb`, `workspace_path`, `created_ts/updated_ts`) — **no thread FK**: global board survives session purge (the `message_feedback` v15 precedent). Do NOT add to `purge_session` table lists.
  - `todo_notes` (`note_id`, `card_id` FK CASCADE, `body`, `author` ∈ user/tinker/master, `created_ts/updated_ts`).
  - `todo_attachments` (`attachment_id`, `card_id` FK CASCADE, `kind` ∈ file/image/video/link/diagram/artifact, `path`/`url`, `mime`, `title`, `meta jsonb`, `created_ts`).
  - `todo_note_chunks` (pgvector, HNSW — copy `transcript_chunks` block) for semantic recall over notes.
- **Stores** (`yuyutsava/todoboard/store.py`): `TodoStore` ABC + `PgTodoStore`/`SqliteTodoStore` twins, mirroring `task_registry.py`/`feedback_store.py`. Wire `RoutedStore(primary=Pg, buffer=Sqlite, health)` + `set_default_todo_store(...)` in `daemon/bootstrap.py` (beside `set_default_feedback_store`) and CLI `agent_stack.py`. Register `TableSpec`s in `CONTENT_TABLE_SPECS` (`storage/routing/reconcile.py`) for spillover drain.
- **Blobs**: attachments + agent artifacts under `blobs_dir()/todoboard/<card_id>/` (also the card's tr_* `workspace_root`). Deleting a card = delete rows (CASCADE) + unlink dir. **No TTL sweep** — durable user data; add only an orphan-dir sweep (dir with no card row) to `UnifiedSweeper`.
- **Semantic recall**: `PgVectorTable` for `todo_note_chunks` + `PgVectorSearch` (copy `memory/store.py:42` pattern); embed notes on write, backfill on boot.

### 2. Exchange protocol (`yuyutsava/todoboard/exchange.py`)
- Versioned Pydantic models: `TodoCardV1`, `TodoNoteV1`, `TodoAttachmentV1`, `TodoCardSummaryV1`, `BoardSnapshotV1` (each carries `schema_version`). This is the **only** contract producers/consumers see — REST schemas, tools, and TinkerAgent all serialize through it; adding V2 later never breaks writers.
- API: `add_card`, `update_card`, `add_note`, `attach`, `get_card`, `query_board(filter)`, `board_snapshot()` — all validate then call the store. `board_snapshot`/`query_board` is the "common protocol" masters use instead of raw data; card rows carry `workspace_path` so any agent can locate artifacts.
- **Typed exceptions**: `TodoError` base → `TodoValidationError`, `TodoNotFoundError`, `TodoStorageError`, `TodoAttachmentError`. Router maps → HTTP 400/404/507/500; tools catch → structured error strings (agent loop never crashes); blob-write failure rolls back the metadata row (write file first, row second, unlink on row failure).

### 3. Tools (`yuyutsava/todoboard/tools.py`)
- New **`todo_*` family** via `make_todo_tools(exchange, scope=...)`: `todo_add`, `todo_list`, `todo_get`, `todo_update`, `todo_add_note`, `todo_attach_artifact`, `todo_set_status`.
  - **Master/CLI scope**: capture subset (`todo_add`, `todo_list`, `todo_get`) — "assign this as a TODO" from any chat/voice/CLI.
  - **Tinker scope**: full set.
- Registered in the `ToolRegistry` (Tier-0 blurbs in `catalog_block()`), hidden behind `tool_search` by adding the `todo_` prefix to `ToolFilterMiddleware`'s prefix list — same lazy-discovery rules as `ws_*`/`vis_*`.

### 4. TinkerAgent (`yuyutsava/agents/tinker/` + `core/engine.py`)
- **`build_tinker_agent(...)`** — third factory in `core/engine.py`, sibling of `build_cli_deepagent`/`build_orchestrator`, returns its own `AgentBundle`. Reuses `_build_tool_registry_and_tools` and `_context_middleware`; composes: `tool_search` gateway, ToolFilterMiddleware, context chain (offload → compaction → transcript RAG), `BudgetMiddleware`/`UsageRecorder`, `PermissionMiddleware`/consent.
- **Tools**: `todo_*` (full), `tr_*` via TaskRunner `bind_tools(workspace_root=<card dir>, agent_name="tinker")` — the gateway rule; `vis_*` (charts/diagrams/Kroki/code-as-image/tables), `ws_*` search, `mem_*`, `ctx_*`, `sk_*` with `agent="tinker"`, MCP tools (user-configured servers, same wiring as master).
- **Subagents**: pass existing `subagents=`/`async_subagents=` (e.g. `GeneralPurposeAgent`) so Tinker can delegate sync/background work; async completion reuses `LaunchIndex` + watcher wake on the card's thread.
- **Prompt** (`agents/tinker/prompts.py`): purpose-built, distinct from orchestrator — thinking partner, not order-taker: take crumbled ideas and return an *improved sharper version* (never rush a full solution), decompose into small objectives, **active HITL** (ask clarifying questions via `ask_user`/interrupts before committing to a direction), persist insights as notes/artifacts on the card, respect specific *modes* (see skills).
- **Skills** (`yuyutsava/skills/bundled/tinker/`): Designing, Thinking (first-principles decomposition), Tinkering (iterate small objectives), Creating (artifact production). `SkillInjector(store, agent="tinker")` — the `agent` column mechanism already exists end-to-end.
- **Threading**: `thread_id = f"todo:{card_id}"`; opening a card resumes its history; transcript RAG (v13 `transcript_chunks`) gives long-horizon recall without bloat.

### 5. Conversation plumbing (chat + voice, same modules)
- **`ConversationManager`** (`daemon/conversation_manager.py`): replace single `self._bundle` with `self._bundles: dict[str, AgentBundle]` + per-agent factory map (`"master"` → `build_agent_stack`, `"tinker"` → tinker stack builder). `open(agent="tinker", thread_id=..., origin=...)` selects the bundle; same lazy build-on-first-use and per-thread checkpointer isolation.
- **`/ws/converse`** (`routers/converse.py`): accept `agent=tinker&card=<card_id>` query params → route to tinker bundle, thread pinned to the card. All existing frames (token/tool_call/image/ask/interrupt, voice transcript/audio_chunk/barge-in) work unchanged — HITL asks reach the UI through the same inline-ask protocol.
- **Voice**: nothing new to build — `VoicePipeline`/VAD/TTS/earcons are per-connection and agent-agnostic; the card view's voice mode passes `agent=tinker`.

### 6. REST API (`daemon/web/routers/todos.py` + `web/schemas/todo.py`)
- CRUD over the exchange: `GET/POST /todos`, `GET/PATCH/DELETE /todos/{id}`, `POST /todos/{id}/notes`, `PATCH/DELETE .../notes/{nid}`, `GET /todos/{id}/snapshot`.
- **Attachment upload — first multipart endpoint in the codebase**: `POST /todos/{id}/attachments` (`UploadFile`, size + mime allowlist validation), `GET /todos/{id}/attachments/{aid}` (`FileResponse`, following `routers/visuals.py`), `DELETE`. Streamed writes into the card dir.
- Add router to `api_routers` in `web/app.py` → auto dual-mount `/v1/todos` + `/todos`. Pydantic schemas are thin wrappers over exchange models.

### 7. Electron UI
- Nav: `{ id: 'todos', label: 'Todos' }` in `NAV_ITEMS` (`components/layout/navIcons.jsx`) + glyph; panel branch in `App.jsx` (stateless remount group).
- **`components/todos/TodosPanel.jsx`**: board of clickable cards (title, status, note/attachment counts), Create/Delete buttons, status columns or filter.
- **`components/todos/TodoCardView.jsx`** (expanded): renamable title; notes list with add-by-typing **and STT** (reuse mic/VAD capture from voice modules for dictation); attachment upload (drag-drop + picker → multipart endpoint); artifact gallery.
- **"Think with TinkerAgent"**: embeds the existing chat surface (`useConverse` hook gains an `agent`/`card` option → `ConverseClient` appends the query params); voice toggle likewise. Chat/voice components are reused, not forked.
- API calls added to `renderer/api/client.js`.

### 8. Pluggable artifact blocks (`yuyutsava/todoboard/artifacts.py` + `components/todos/artifactBlocks/`)
- Backend: `ArtifactBlock` descriptor (`kind`, mime set, validator, optional `generate(spec) -> path`) in a registry dict; `attach()` dispatches by kind. v1 kinds: `text/md/html` file, `image`, `link`, `diagram/chart/code-image` (delegates to existing `vis_*` `render()`), generic `file`.
- Frontend: renderer registry keyed by `kind` — one module per kind (`TextBlock.jsx`, `ImageBlock.jsx`, `LinkBlock.jsx`, `DiagramBlock.jsx`); unknown kinds fall back to a download tile. **Adding JSX-sandbox or audio blocks later = one new module + one registry entry on each side, zero core edits.**

## Phases

**Phase 1 — Foundations (storage + protocol + tools)**
Migration v16, `todoboard/` package (models, exchange + exceptions, stores, RoutedStore/TableSpec wiring in bootstrap + agent_stack), `todo_*` tools registered for the master/CLI (capture works from any chat immediately), REST router (CRUD, no upload yet). *Verify: standalone python checks on exchange/store round-trip (both backends), curl the REST CRUD.*

**Phase 2 — Board UI**
TodosPanel + TodoCardView (title/notes/status), nav entry, client API. *Verify: vite build + manual Electron run — create/rename/note/delete cards.*

**Phase 3 — TinkerAgent + chat**
`agents/tinker/` (prompt, agent), `build_tinker_agent` factory, multi-bundle `ConversationManager`, `/ws/converse?agent=tinker&card=`, per-card threads, "Think with TinkerAgent" chat in card view, tinker skills namespace, HITL asks inline. *Verify: open a card, refine an idea, agent asks questions, adds notes via `todo_*`.*

**Phase 4 — Attachments + artifact blocks**
Multipart upload endpoint + blob layout + rollback handling, artifact-block registries (backend + frontend), `vis_*`-backed diagram/chart generation onto cards, orphan-dir sweep. *Verify: upload image/file, ask agent for a diagram from data, see it render on the card.*

**Phase 5 — Voice + STT dictation**
Voice mode in card view (`agent=tinker` over existing voice frames), STT note dictation in the note editor. *Verify: voice-tinker a card end-to-end.*

**Phase 6 — Master delegation + MCP + note recall**
TinkerAgent registered via `as_async_subagent_spec()` so the master can delegate long tinkering jobs (completion wake via existing `OrchestratorTask(kind=subagent_completed)`); asks surface in UI through ChannelRouter; user-configured MCP design servers into the tinker registry; `todo_note_chunks` semantic recall live. *Verify: from master chat, delegate "tinker on card X in background", get woken summary.*

**Phase 7 — Advanced artifact blocks**
JSX-sandbox renderer (sandboxed iframe/webview, CSP, no remote fetch) and audio artifacts (TTS pipeline) — each as a new block module proving the pluggability claim.

## Conventions
- Track progress in `docs/TODO_BOARD_PLAN.md` + progress file (mirrors the `docs/MASTER_PLAN.md` protocol); commit locally per phase, never push; pause for review between phases.
- Testing: fast standalone python checks (no full pytest / app-importing tests — slow langgraph import), `vite build` for renderer changes, manual daemon/Electron runs for e2e.

## Key files touched (representative)
- New: `yuyutsava/todoboard/{models,exchange,store,tools,artifacts}.py`, `yuyutsava/agents/tinker/{agent,prompts}.py`, `yuyutsava/skills/bundled/tinker/*`, `daemon/web/routers/todos.py`, `web/schemas/todo.py`, `electron-app/src/renderer/components/todos/*`.
- Modified: `storage/pg/migrations.py` (v16), `storage/routing/reconcile.py` (TableSpecs), `daemon/bootstrap.py` + `cli/agent_stack.py` (store wiring, tools), `core/engine.py` (`build_tinker_agent`), `core/tool_filter_middleware.py` (`todo_` prefix), `daemon/conversation_manager.py` (multi-bundle), `daemon/web/routers/converse.py` (`agent` param), `daemon/web/app.py` (router), `renderer` nav/`App.jsx`/`api/client.js`/`useConverse`/`converse.js`.
