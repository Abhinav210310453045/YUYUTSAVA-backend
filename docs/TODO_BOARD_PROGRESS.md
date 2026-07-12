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

## Phase 4 — Attachments + artifact blocks
Not started.

## Phase 5 — Voice + STT dictation
Not started.

## Phase 6 — Master delegation + MCP + note recall
Not started.

## Phase 7 — Advanced artifact blocks
Not started.
