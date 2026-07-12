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

## Phase 2 — Board UI
Not started.

## Phase 3 — TinkerAgent + chat
Not started.

## Phase 4 — Attachments + artifact blocks
Not started.

## Phase 5 — Voice + STT dictation
Not started.

## Phase 6 — Master delegation + MCP + note recall
Not started.

## Phase 7 — Advanced artifact blocks
Not started.
