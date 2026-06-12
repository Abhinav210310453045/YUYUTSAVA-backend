# YUYUTSAVA Daemon API — /v1 contract

> **Phase 6 freeze.** This document is the source of truth for clients
> (mobile app, scripts). The canonical surface is `/v1/...`; every endpoint
> also answers on the legacy unprefixed path (`/tasks` ≡ `/v1/tasks`) for
> the Electron renderer, which predates the freeze. New clients MUST use
> `/v1`. `/openapi.json` documents only the `/v1` surface — generate typed
> clients from it.

- Base URL: `http://<host>:7654` (default port; loopback for dev,
  tailnet IP e.g. `http://100.x.y.z:7654` for the mobile app).
- All bodies are JSON. All timestamps are **epoch seconds as floats**
  unless noted.

## Authentication

| Bind | Rule |
|---|---|
| Loopback (`127.*`, `localhost`, `::1`) | No auth (Electron unchanged) |
| Non-loopback (e.g. tailnet) | `Authorization: Bearer <token>` on every request |

- Token source: `YUYUTSAVA_API_TOKEN` env; auto-generated to
  `~/.yuyutsava/api_token` (0600) on first non-loopback boot when unset.
- `GET /health` and `GET /v1/health` are always public (reachability probe
  — the app validates the server URL before asking for a token).
- `GET /stream` / `GET /v1/stream` additionally accept `?token=<token>`
  in the query string, because `EventSource` cannot set headers. No other
  path accepts a query token. Inside the tailnet this is defense-in-depth
  (Tailscale ACLs are the outer wall); the token is never written to logs.
- Failure: `401 {"code":"unauthorized","message":"missing or invalid bearer token","details":{}}`.

## Error envelope

Every application error has the shape:

```json
{"code": "not_found | validation_error | conflict | service_unavailable | http_error | internal_error",
 "message": "human-readable",
 "details": {}}
```

Status codes: 404 not_found · 409 conflict · 422 validation_error
(body/param validation; `details.errors` carries pydantic errors) ·
503 service_unavailable (subsystem not wired) · 401 unauthorized.

## Pagination

Keyset (cursor) pagination, newest-first, on the three list endpoints:

| Endpoint | `cursor` type | How to get the next page |
|---|---|---|
| `GET /v1/tasks` | string (`task_id`) | response carries explicit `next_cursor`; pass it back; `null` ⇒ end |
| `GET /v1/sessions` | float (`updated_at`) | pass the **last row's** `updated_at`; fewer than `limit` rows ⇒ end |
| `GET /v1/decisions` | float (`ts`) | pass the **last row's** `ts`; fewer than `limit` rows ⇒ end |

---

## Endpoints

### Meta

#### `GET /v1/health` — liveness probe (public)
`200 {"status": "ok", "ts": 1781269892.05}`

#### `GET /v1/server-info` — capabilities for graceful degradation
```json
{
  "name": "yuyutsava",
  "version": "0.1.0",
  "api_version": "v1",
  "capabilities": {
    "model_routing": true,      // YUYUTSAVA_MODEL_ROUTING=1 → tasks carry complexity/model
    "memory": true,             // semantic memory store wired
    "resource_governor": true,  // /system/metrics live + system_metrics SSE events
    "async_subagents": false    // async_task_* SSE events may appear when true
  },
  "channels": [                 // ChannelPluginRegistry snapshot (Settings screen)
    {"name": "telegram", "available": true, "enabled": false,
     "running": false, "capabilities": ["notify","proposal","ask","invoke"]}
  ]
}
```

### Tasks

Task lifecycle: **`queued` → `running` → `done` | `failed` | `cancelled`**.
(`queued` covers admission deferral — a heavy task held back by the
resource governor stays `queued` until it actually starts. A `triage`-mode
submission the user skips stays `queued`.)

#### `POST /v1/tasks` — submit a task
Request:
```json
{"instruction": "summarize ~/Downloads",   // required, 1..20000 chars
 "mode": "direct",                          // "direct" (default; runs now) | "triage" (LLM triage + Tier-1 consent)
 "origin": "api",                           // submitting surface, recorded on the row (default "api")
 "complexity": 3}                           // optional 1–5 override; omitted ⇒ scored by a light-tier model when routing is on
```
Response: `200 {"task_id": "tsk_01J...", "mode": "direct"}` (both modes).

#### `GET /v1/tasks?status=&limit=&cursor=` — list newest-first
- `status`: optional filter, one of the five lifecycle states (unknown ⇒ 404).
- `limit`: 1–200, default 50. `cursor`: `task_id` from previous `next_cursor`.
```json
{"tasks": [ /* TaskOut, see below */ ], "next_cursor": "tsk_01J... | null"}
```

#### `GET /v1/tasks/{task_id}` — one task (TaskOut)
```json
{"task_id": "tsk_01J...", "origin": "api", "instruction": "...",
 "status": "done", "created_ts": 1781269892.0,
 "thread_id": "orch-...",          // join key for /stream?session_id= (null until running)
 "complexity": 3,                   // null = never scored
 "model": "claude-haiku-...",       // model the router chose (null when routing off)
 "started_ts": 1781269893.0, "finished_ts": 1781269940.2,  // null until reached
 "deferred_ms": 0,                  // how long admission held it back
 "result_summary": "…final text…",  // on done
 "error": null}                     // on failed
```

#### `POST /v1/tasks/{task_id}/cancel`
`200 {"ok": true, "note": "cancellation requested; honored at the next stream event"}`
— coarse v1: the orchestrator checks between stream events. `404` unknown
task, `409` already finished.

#### `GET /v1/tasks/{task_id}/events` — replay ring buffer
Returns the task's recent stream items (last 500) **in the same wire
envelopes the SSE stream emits** — call on (re)connect to fill the gap,
then resume `GET /v1/stream?task_id=`:
```json
{"task_id": "tsk_01J...", "events": [ /* SSE wire envelopes, oldest first */ ]}
```
`404` unknown task. Note: the ring is in-memory — empty after a daemon
restart and evicted oldest-task-first past 64 tracked tasks.

### Live stream (SSE)

#### `GET /v1/stream?token=&task_id=&session_id=`
`text/event-stream`. Not in `/openapi.json` (SSE; hand-write the client).
- `?token=` — bearer token (EventSource can't set headers; only here).
- `?task_id=` — only items tagged with that task. **Proposals/asks carry no
  task tag and are filtered out** — filter by `?session_id=` (the run's
  `thread_id` from TaskOut) to receive asks scoped to one run, or run an
  unfiltered stream for approvals.
- First frame is always `event: hello`, `data: {"ts": ...}`.
- Each SSE message's `event:` field equals the envelope's `type`
  (`event` | `proposal` | `ask`); `data:` is the JSON envelope.

#### Wire envelopes (`StreamItem.to_wire_dict()`)

**1. `type: "event"`** — relay of a ChannelEvent:
```json
{"type": "event",
 "kind": "<payload kind, see table>",
 "task_id": "tsk_01J... | null",      // null = unscoped (boot notices, http logs)
 "session_id": "orch-... | null",
 "data": { /* kind-specific fields, 'kind' key removed */ }}
```

| `kind` | `data` fields | Meaning |
|---|---|---|
| `log` | `text` | one-line status message |
| `token` | `text` | streaming AI text chunk |
| `tool_call` | `name`, `args{}` | model called a tool |
| `tool_result` | `name`, `preview` | tool returned (preview pre-truncated) |
| `timeline` | `line`, `cls`, `ts` | structured timeline row; `cls` is a CSS-ish class hint (`event-action`, `event-error`, …) |
| `http_log` | `method`, `path`, `status`, `duration_ms`, `ts` | daemon HTTP access log (path only, never query strings) |
| `system_metrics` | `cpu_pct`, `mem_available_mb`, `disk_free_gb`, `ts` | Phase-5 load sample; emitted ≤ once per 10 s while a task runs |
| `async_task_started` | `task_id`, `agent_name`, `instruction_preview`, `ts` | background subagent launched |
| `async_task_progress` | `task_id`, `agent_name`, `kind_hint` (`status_change`\|`log`), `text`, `ts` | background task progress |
| `async_task_awaiting_user` | `task_id`, `agent_name`, `ask_id`, `title`, `ts` | background graph hit interrupt() |
| `async_task_completed` | `task_id`, `agent_name`, `ok`, `summary`, `duration_sec`, `ts` | background task terminal |

(`async_task_*` only when `capabilities.async_subagents`; `system_metrics`
only when `capabilities.resource_governor`.)

**2. `type: "proposal"`** — a Tier-1 proposal awaits a decision:
```json
{"type": "proposal",
 "proposal": {
   "proposal_id": "01J...", "event_id": "01J...", "topic": "fs.downloads.new",
   "summary": "New file report.pdf", "proposed": "Summarize the new file",
   "subagent": "general-purpose", "urgency": 2,
   "created_ts": 1781269892.0, "expires_ts": 1781270192.0,
   "status": "pending",
   "session_id": "orch-... | null", "agent_path": "orchestrator | null"}}
```

**3. `type: "ask"`** — a Tier-2 ask (tool permission / orchestrator question):
```json
{"type": "ask",
 "ask": {"ask_id": "01J...", "title": "Permission: shell",
         "body": "Run `rm -rf build/`?",
         "options": ["approve", "reject"],   // empty array ⇒ free-text reply expected
         "session_id": "orch-... | null",     // thread_id of the run (use for ?session_id=)
         "agent_path": "orchestrator/file_organizer#1 | null"}}
```

### Approvals

#### `POST /v1/proposal/{proposal_id}/respond`
Request: `{"decision": "approve" | "approve_remember" | "modify" | "skip" | "skip_remember", "edited_instruction": "… | null"}`
(`edited_instruction` only meaningful with `modify`.)
`200 {"ok": true, "note": null | "no listener (already resolved)"}` ·
`409 conflict` when expired/already answered.

#### `POST /v1/ask/{ask_id}/respond`
Request: `{"response": "approve"}` — free text; for option asks send one of
the offered options. Empty/whitespace defaults to `"reject"`.
`200 {"ok": true}` · `409 conflict` when the ask is gone.

### History

#### `GET /v1/sessions?workspace=&limit=&cursor=` — persisted CLI/daemon sessions
`limit` 1–500 (default 50), `cursor` = previous page's last `updated_at`.
Returns a **bare array**, newest-updated first:
```json
[{"id": "cli-...", "thread_id": "cli-...", "workspace": "/Users/me/proj",
  "status": "running", "created_at": 1781269892.0, "updated_at": 1781270000.0,
  "message_count": 12, "memory_files_count": 0, "db_row_bytes": 20480,
  "task_preview": "first 160 chars of the task…", "schema_version": 1}]
```

#### `GET /v1/sessions/{session_id}` — one session · `404` unknown
#### `DELETE /v1/sessions/{session_id}` — delete session + its checkpoint rows
`200 {"deleted": 1}`

#### `GET /v1/decisions?limit=&cursor=` — decision audit log
`limit` 1–500 (default 50), `cursor` = previous page's last `ts`.
Returns a **bare array**, newest first:
```json
[{"decision_id": "01J...", "proposal_id": "01J... | null", "event_id": "01J...",
  "outcome": "approved | skipped | orchestrator_done | …",
  "action_summary": "… | null", "ts": 1781269892.0,
  "session_id": "orch-... | null", "agent_path": "orchestrator | null"}]
```

### Dashboards

#### `GET /v1/system/metrics` — load snapshot + history + attribution
`503` when the resource governor is off (check `server-info`).
```json
{"current": {"cpu_pct": 12.5, "mem_available_mb": 4096.0,
             "disk_free_gb": 200.0, "ts": 1781269892.0, "per_container": {}},
 "loaded": false, "disk_critical": false,
 "ring": [ /* snapshots, oldest first, ~10 min at 5 s cadence */ ],
 "heavy_slots": {"max": 1, "in_use": 0},     // null when no admission controller
 "active_tasks": [{"task_id": "tsk_...", "weight": "heavy",
                   "deferred_ms": 6000, "since": 1781269890.0}]}
```

#### `GET /v1/usage?since=&group_by=` — LLM spend aggregates
`since` epoch-seconds lower bound; `group_by` ∈ `task` | `model` | `day`
(omit ⇒ one `key:"all"` totals row). `422` bad group_by, `503` store unwired.
```json
{"since": null, "group_by": "model",
 "rows": [{"key": "claude-haiku-4-5", "calls": 12, "input_tokens": 84000,
           "output_tokens": 9100, "est_cost_usd": 0.1034}]}
```
Rows are ordered most-expensive first.

### Channels (Settings screen)

#### `GET /v1/channels` — `{"channels": [ChannelInfo…]}` (same rows as server-info)
#### `POST /v1/channels/{name}/enable` — hot-enable + persist
`200 {"ok": true, "name": "telegram", "running": true, "changed": true}`
(`changed: false` ⇒ idempotent no-op) · `404` unknown plugin ·
`422` misconfigured (e.g. missing `YUYUTSAVA_TELEGRAM_BOT_TOKEN`) — fix env
and retry, nothing was persisted.
#### `POST /v1/channels/{name}/disable` — hot-disable + persist

### Consent rules

#### `GET /v1/rules` — bare array of active consent rules
`[{"rule_id": "01J...", "topic_glob": "fs.*", "match_json": "{}", "decision": "auto_approve | auto_skip", "created_ts": ..., "expires_ts": null}]`
#### `DELETE /v1/rules/{rule_id}` — `200 {"deleted": 1}`

### Skills

#### `GET /v1/skills` — `[{"name", "description", "scope", "agent"}]` (empty when registry off)
#### `DELETE /v1/skills/{name}` — delete a personal-scope skill · `404` unknown

### Daemon config & ops (desktop UI; mobile does not need these)

| Endpoint | Purpose |
|---|---|
| `GET /v1/config/events` / `PATCH /v1/config/events` | read / replace events config (hot reload) |
| `POST /v1/config/events/roots` / `DELETE /v1/config/events/roots?path=` | add / remove watched directory |
| `GET /v1/logs/level` / `PUT /v1/logs/level` | get / set daemon log level (`DEBUG`/`INFO`/`WARNING`) |
| `POST /v1/cli/attach` / `POST /v1/cli/detach` | CLI Mode-2 remote-channel attach |
| `GET /v1/db/databases`, `GET /v1/db/{db}/tables`, `GET /v1/db/{db}/tables/{table}/schema`, `POST /v1/db/{db}/query` | read-only SQLite introspection (off when `YUYUTSAVA_DB_API_ENABLED=false`) |

---

## Client recipes

**Mobile task-detail reconnect** (the replay-fill contract):
1. `GET /v1/tasks/{id}` → status + `thread_id`.
2. `GET /v1/tasks/{id}/events` → render the ring replay (oldest first).
3. Open `GET /v1/stream?token=…&task_id={id}` and append live items.
4. On disconnect/app-kill: reopen by repeating 2–3 — the ring covers the gap.

**Approvals screen**: keep one unfiltered SSE stream open while
foregrounded; `type: "proposal"` / `type: "ask"` frames carry everything
needed to render buttons; answer via the respond endpoints; a `409` means
another surface (Electron, Telegram) answered first — refresh and move on.

**Capability gating**: call `GET /v1/server-info` after the `/health`
probe; hide the metrics dashboard unless `resource_governor`, the
model/complexity chips unless `model_routing`, and the background-tasks
panel unless `async_subagents`.
