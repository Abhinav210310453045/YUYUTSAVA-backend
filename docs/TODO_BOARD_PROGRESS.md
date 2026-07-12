# TODO Board + TinkerAgent — Implementation Progress

Plan: `docs/TODO_BOARD_PLAN.md`. Protocol: implement phase-by-phase, commit
locally per phase (never push), pause for user review between phases.

## Phase 1 — Foundations (storage + protocol + tools + REST CRUD) — DONE 2026-07-12
- [x] PG migration v16 (`todo_cards`, `todo_notes`, `todo_attachments`, `todo_note_chunks`) — applied to the live pgvector DB (schema_meta now v16)
- [x] `yuyutsava/todoboard/models.py` — versioned exchange schemas (V1)
- [x] `yuyutsava/todoboard/exchange.py` — protocol + typed exceptions + per-card workspace (`blobs/todoboard/<card_id>/`)
- [x] `yuyutsava/todoboard/store.py` — `TodoStore` ABC + Pg/Sqlite twins + default-store singleton
- [x] RoutedStore wiring in `daemon/bootstrap.py`; Pg wiring in `cli/agent_stack.py`; `TableSpec`s in `storage/routing/reconcile.py`
- [x] `yuyutsava/todoboard/tools.py` — `todo_*` family (capture scope wired into CLI deepagent + orchestrator; full scope ready for TinkerAgent)
- [x] `todo_` prefix in `ToolFilterMiddleware`; names visible in ToolRegistry catalog, schemas via `tool_search`
- [x] REST router `daemon/web/routers/todos.py` + `web/schemas/todo.py` + mount in `web/app.py` (dual `/v1/todos` + `/todos`)
- [x] Verified: 41 SQLite exchange/store/tool checks, 10 PG round-trip checks (jsonb/epoch/CASCADE), 18 REST TestClient checks, live daemon curl CRUD (create→list→get→patch→note→snapshot→404s→delete, workspace dir cleaned)
- Note: "assign this as a TODO" through a real LLM chat turn is a manual review item (tools are registered + individually exercised)

## Phase 2 — Board UI — DONE 2026-07-12
- [x] `todos` nav entry + check-square glyph in `NAV_ITEMS` (`components/layout/navIcons.jsx`); panel branch in `App.jsx` (stateless remount group)
- [x] `components/todos/TodosPanel.jsx` — four status columns (inbox/active/done/archived) with per-status accents, clickable summary cards (title, tags, pinned pin, note/attachment counts, age), create input + Add, per-card delete (confirm), 5s poll while the board is visible (agents write TODOs too)
- [x] `components/todos/TodoCardView.jsx` — expanded card: renamable title (Enter/blur commits, Esc reverts), status select, pin toggle, notes list with per-author badges + add (⌘/Ctrl+Enter) / inline edit / delete
- [x] `components/todos/shared.jsx` — STATUS_ACCENT / TagChips / PinIcon / humanAge shared by both views
- [x] `api/client.js` — listTodos/createTodo/getTodo/patchTodo/deleteTodo + note add/patch/delete on the unprefixed legacy routes; `_json` now returns null on 204 (the todo DELETEs)
- [x] Verified: `vite build` clean; live-daemon curl sequence of the exact renderer flows (create→list→rename→status+pin→note add/edit→delete note→delete card→404); real Electron run driven via Playwright — 10/10 UI checks passed (nav icon, board columns, create, open, rename, status, note add/edit/delete, card delete) with screenshots reviewed
- Note (STT dictation is Phase 5, attachment upload is Phase 4 — deliberately absent)

## Phase 3 — TinkerAgent + chat — DONE 2026-07-12
- [x] `yuyutsava/agents/tinker/` — `prompts.py` (purpose-built thinking-partner prompt: sharpen-don't-solve, small objectives, active HITL via tr_ask_user, persist insights via todo_*; reuses the CLI's TOOL DISCOVERY/ZONES blocks + host profile) and `agent.py` (`build_tinker_stack`: retrieval stores via `_build_retrieval_stores`, PG/SQLite context stores, GeneralPurposeAgent sync subagent working in the card workspace, env-gated async host attach; does NOT touch the default todo store — bootstrap's RoutedStore stands)
- [x] `build_tinker_agent(...)` — third factory in `core/engine.py`. One bundle per card (tr_* bound to the card workspace via `bind_tools(agent_name="tinker")`, card identity baked into the prompt); full yuyutsava stack: tool_search gateway + ToolFilterMiddleware, context chain (offload → compaction → transcript RAG) role="tinker", BudgetMiddleware(role="tinker") + UsageRecorder (task_id `tinker:<card>`), PermissionMiddleware on the card workspace, RetrievalInjection (memory + SkillInjector(agent="tinker") + conversation recall). Tools: todo_* FULL (author="tinker"), tr_*, vis_* (output → card `_output/`), ws_*, mem_*, ctx_*, sk_*
- [x] Multi-bundle `ConversationManager` — `self._bundles: dict[str, AgentBundle]` keyed `"master"` / `"tinker:<card_id>"`, per-agent factory map, per-key build locks, same lazy build-on-first-use; tinker sessions pinned to `thread_id = "todo:<card_id>"` (session id == thread id, so the pin doubles as the resume key); card validated via the exchange before any build; `usage_store` threaded from bootstrap
- [x] `/ws/converse?agent=tinker&card=<id>` — routes to the card bundle; hello frame carries agent/card; all existing frames (token/tool_call/image/ask/interrupt) unchanged — HITL asks flow through the existing inline-ask protocol
- [x] Tinker skills namespace — `skills/bundled/tinker/{thinking,designing,tinkering,creating}/SKILL.md`; agent-scoped via the bundled-dir mechanism (invisible to orchestrator scope), indexed into the semantic store by the stack build's SkillIndexer sync, surfaced per-turn by `SkillInjector(store, agent="tinker")`
- [x] "Think with TinkerAgent" — ChatPanel parameterized (agent/card/origin/title/placeholder/emptyHint/showVoice/showNewSession/onTurnEnd), reused not forked; `useConverse` + `ConverseClient` gained agent/card (appended as query params); TodoCardView "✦ Tinker" header toggle splits the view into notes | chat, `onTurnEnd` refreshes the notes pane, `resumeId=todo:<card>` hydrates past turns on reopen. Voice toggle deliberately absent (Phase 5)
- [x] Verified: 29 standalone python checks (prompt render, skills namespace + agent scoping, compiled graph with todo_*/tr_*/vis_*/ws_*/tool_search registered, manager key/factory/validation logic); vite build clean; live WS smoke (fresh open → tool_search → tr_ask_user ask answered inline → todo_add_note → reconnect resuming=True with memory of the agreed scope); real Electron run via Playwright — board → card → ✦ Tinker → hydrated history → clarifying Question box answered inline → second tinker note appeared on the card (screenshots reviewed) → close/reopen resumed the thread; notes confirmed via REST with author="tinker"
- Note: demo card `tdo_01KXB245YKADRSYZK40TS9Q483` ("Phase-3 tinker e2e card") left on the board for review

## Phase 4 — Attachments + artifact blocks — DONE 2026-07-12
- [x] `yuyutsava/todoboard/artifacts.py` — pluggable artifact-block registry: `ArtifactBlock` (name, storage kind, mimes, upload_mimes, validator, optional `generate(spec, out_dir)` delegating to `visuals.render`); dispatch by (kind, mime). Storage `kind` keeps the closed V1 vocabulary (versioned schema + DB CHECK); new blocks (Phase 7 JSX/audio) ride on `artifact`/`file` kinds via mime — proven in checks by registering an `audio` block with zero core edits. `.md → text/markdown` registered (Python <3.13 gap)
- [x] `exchange.attach()` now dispatches validation through the registry (lazy import breaks the exceptions cycle); validators run off-loop and infer missing mimes from the file suffix
- [x] Multipart REST (first in the codebase) in `routers/todos.py`: `POST /todos/{id}/attachments` (UploadFile + optional title/kind Form fields; 50 MB cap enforced mid-stream → 413; registry mime allowlist → 415; kind inferred from mime; streamed 1 MB chunks into the card workspace; rollback covenant: file first, row second, unlink on ANY failure incl. 413), `GET .../attachments/{aid}` (FileResponse à la visuals.py, `?download=true` sets Content-Disposition), `DELETE` (exchange unlinks workspace-resident files). Auto dual-mounted `/v1/todos` + `/todos`
- [x] `list_card_ids` on `TodoStore` ABC + both twins + exchange (RoutedStore proxies via `__getattr__`)
- [x] Orphan-dir sweep in `UnifiedSweeper`: removes `blobs/todoboard/<dir>` with no card row; NO TTL (durable user data); 1h mtime grace covers the exchange's mkdir-before-row ordering; card ids via the exchange only; sweep skipped while storage degraded (buffer card list is partial) or when the id listing fails; wired in bootstrap with `get_default_exchange()` + `storage_health`
- [x] Frontend `components/todos/artifactBlocks/` — ordered `matches(att)` registry (`index.js`): DiagramBlock, ImageBlock, TextBlock (fetches + previews text, source only — never DOM-injected), LinkBlock; unknown kinds (artifact/video/…) fall back to DownloadTile (name, kind·mime·size, Download link)
- [x] TodoCardView: Attachments gallery in the notes column (coexists with the Phase-3 notes|chat split) — drag-drop zone + hidden picker → `uploadTodoAttachment`, per-tile kind badge/title/age/Delete (confirm), auto-fill grid; `client.js` gained uploadTodoAttachment (FormData, surfaces 413/415 detail), todoAttachmentUrl, deleteTodoAttachment
- [x] Verified: 68 standalone python checks (registry resolution/allowlist/kind-inference/pluggability, exchange dispatch on SQLite, TestClient multipart incl. 413-no-partial-file + row-failure-rollback + cross-card 404s + filename collision, orphan sweep incl. degraded/failed-listing skips); PG round-trip via live daemon curl (upload png/md, GET bytes-identical, download disposition, 415, /v1 mount) + exchange-over-PG artifact attach + `list_card_ids`; vite build clean; real Electron runs via Playwright — gallery blocks (image decoded, md text preview, artifact→download tile), picker + drag-drop uploads, targeted attachment delete, "✦ Tinker → vis_diagram (graphviz) → todo_attach_artifact kind=diagram" landed on the card and renders as a DIAGRAM tile (screenshots reviewed)
- Note: e2e card `tdo_01KXBHWGQAE9Y9FBZTAM56GENX` ("Phase-4 attachments e2e card", 4 attachments incl. the tinker diagram) left on the board for review; the Phase-3 demo card was deleted by the user mid-verification

## Phase 5 — Voice + STT dictation
Not started.

## Phase 6 — Master delegation + MCP + note recall
Not started.

## Phase 7 — Advanced artifact blocks
Not started.
