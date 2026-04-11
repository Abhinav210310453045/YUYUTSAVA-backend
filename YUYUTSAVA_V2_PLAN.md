# YUYUTSAVA v2.0 — Development Plan

> Branch: `yuyutsava_v2.0`
> Status: Planning
> Last Updated: 2026-04-11

---

## 1. What We're Building & Why

YUYUTSAVA v1.0 is a working CLI tool that invokes a DeepAgents/LangGraph agent to execute natural language tasks over a filesystem and shell. It works well as a one-shot tool.

v2.0 expands it into a **full-stack, client-accessible agent platform** with:

- A **REST + WebSocket API** so any UI (web, desktop, mobile) can talk to the agent
- An **async task system** so multiple sessions run concurrently without blocking
- **Short-term memory** (session state, fast-access, per-user/thread)
- **Long-term memory** (persistent, cross-session, cross-restart)
- **Durable execution** so agent runs can survive crashes and resume from the last checkpoint
- A **modular folder structure** — nothing tightly coupled; every concern in its own module

The core CLI (`yuyutsava`) and the `Backend/yuyutsava/` package are **not changed**. Everything new lives in separate top-level modules.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENT / UI                             │
│              (web app, desktop app, future mobile)              │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP REST + WebSocket
┌────────────────────────────▼────────────────────────────────────┐
│                      api-gateway/                               │
│         FastAPI app — session endpoints, streaming              │
│         Auth (API key or OAuth), rate limiting                  │
└──────┬──────────────────────────────────────┬───────────────────┘
       │                                      │
┌──────▼──────────┐                 ┌─────────▼────────────────┐
│  task-runner/   │                 │     memory-service/       │
│                 │                 │                           │
│  Async worker   │                 │  Short-term: Redis        │
│  that invokes   │◄───────────────►│  Long-term:  Postgres     │
│  YUYUTSAVA      │                 │  (both via Docker locally)│
│  with durable   │                 │                           │
│  execution +    │                 │  DeepAgents CompositeBackend│
│  checkpointing  │                 │  wires these together     │
└──────┬──────────┘                 └───────────────────────────┘
       │
┌──────▼──────────────────────────────────────────────────────────┐
│                   Backend/yuyutsava/  (unchanged)               │
│   engine.py  ·  cli.py  ·  core/  ·  docker_sandbox/           │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Repository Folder Structure

Everything new is at the root `/YUYUTSAVA/` level, peer to `Backend/`.

```
YUYUTSAVA/
│
├── Backend/                        ← UNCHANGED (v1.0 CLI)
│   └── yuyutsava/
│       ├── core/engine.py
│       ├── core/config.py
│       ├── cli/cli.py
│       └── ...
│
├── api-gateway/                    ← NEW: REST + WebSocket server
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── .env.example
│   └── src/
│       ├── main.py                 ← FastAPI app entrypoint
│       ├── routes/
│       │   ├── sessions.py         ← POST /sessions, GET /sessions/{id}
│       │   ├── tasks.py            ← POST /tasks, GET /tasks/{id}/stream
│       │   └── health.py
│       ├── schemas/
│       │   ├── session.py
│       │   └── task.py
│       ├── auth/
│       │   └── api_key.py
│       └── dependencies.py
│
├── task-runner/                    ← NEW: Async agent invocation worker
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── .env.example
│   └── src/
│       ├── worker.py               ← Main async worker loop
│       ├── invoker.py              ← Wraps Backend/yuyutsava engine
│       ├── durable.py              ← Checkpoint + resume logic
│       └── events.py               ← SSE/WebSocket event emitter
│
├── memory-service/                 ← NEW: Memory layer (short + long term)
│   ├── pyproject.toml
│   ├── docker-compose.yml          ← Postgres + Redis containers
│   ├── .env.example
│   └── src/
│       ├── short_term.py           ← Redis-backed session store
│       ├── long_term.py            ← Postgres-backed persistent store
│       ├── composite.py            ← CompositeBackend wiring
│       └── migrations/             ← Alembic migrations for Postgres
│
├── infra/                          ← NEW: Docker Compose for full local stack
│   ├── docker-compose.yml          ← All services: api, worker, redis, postgres
│   └── .env.example
│
├── frontend/                       ← EXISTING (OAuth demo, to be evolved)
│
└── YUYUTSAVA_V2_PLAN.md            ← This file
```

---

## 4. Module Details

---

### 4.1 `api-gateway/` — The Client-Facing Server

**Tech:** FastAPI + Uvicorn (already in Backend's requirements)

**Responsibilities:**
- Accept task submissions from any UI
- Manage sessions (create, list, get status)
- Stream agent output back to the client over **Server-Sent Events (SSE)** or **WebSocket**
- Forward tasks to the `task-runner` worker
- Auth via API key header (expandable to OAuth later)

**Key Endpoints:**

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/sessions` | Create a new session (returns `session_id`, `thread_id`) |
| `GET` | `/sessions/{session_id}` | Get session status + history |
| `POST` | `/sessions/{session_id}/tasks` | Submit a task to an existing session |
| `GET` | `/sessions/{session_id}/tasks/{task_id}/stream` | SSE stream of agent output |
| `GET` | `/health` | Health check |

**Key Design Decisions:**
- Sessions map 1:1 to LangGraph `thread_id` — this is the durable execution anchor
- The API does **not** invoke the agent directly — it enqueues and the `task-runner` consumes
- All streaming is **async** — `asyncio` generators piped to SSE
- Stateless server — session state lives in Redis/Postgres, not in RAM

---

### 4.2 `task-runner/` — Async Agent Worker

**Tech:** Python `asyncio`, wraps `Backend/yuyutsava/core/engine.py`

**Responsibilities:**
- Pull tasks from the queue (Redis list or asyncio queue)
- Invoke `build_agent()` + the compiled graph asynchronously via `.ainvoke()` or `.astream()`
- Manage per-session thread IDs for durable execution
- Emit intermediate events (tool calls, partial responses) back to the API layer via Redis pub/sub

**How it wires into YUYUTSAVA:**

```python
# task-runner/src/invoker.py

import sys
sys.path.insert(0, "/path/to/Backend")   # or installed as package

from yuyutsava.core.engine import build_agent
from yuyutsava.core.config import llm_settings_from_env

async def run_task(thread_id: str, task: str, checkpointer, store):
    settings = llm_settings_from_env()
    bundle = build_agent(
        workspace_root=...,
        settings=settings,
        execution_mode="local",     # or "docker"
    )
    
    # Pass checkpointer + store for durable execution
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 200,
    }
    
    async for event in bundle.agent.astream(
        {"messages": [HumanMessage(content=task)]},
        config=config,
        stream_mode="updates",
    ):
        yield event   # streamed back to client
```

**Durable Execution Hook:**
- `checkpointer` is passed into the graph at invocation time
- LangGraph automatically saves state after each node step
- On restart or resume: pass the same `thread_id` with an empty input — graph resumes from last checkpoint

---

### 4.3 `memory-service/` — Short-Term & Long-Term Memory

This is the most important design decision. Here's the full strategy:

---

#### 4.3.1 Short-Term Memory

**Definition:** State that is relevant within an active session — current messages, tool call history, pending tasks, agent working memory. Fast, ephemeral, session-scoped.

**Best Scalable Practice: Redis-backed LangGraph Checkpoint Store**

Why Redis:
- Sub-millisecond read/write latency
- Native TTL support (sessions auto-expire after N hours)
- Horizontally scalable (Redis Cluster for production)
- Pub/Sub built-in (used by task-runner to push events to API layer)
- Drop-in replacement for in-memory `MemorySaver` — same LangGraph interface

**Implementation:**
```python
# memory-service/src/short_term.py

from langgraph.checkpoint.redis import RedisSaver   # pip: langgraph-checkpoint-redis

def get_short_term_checkpointer():
    return RedisSaver.from_conn_string(
        "redis://localhost:6379",
        ttl={"default": 60 * 60 * 24}   # 24-hour session TTL
    )
```

**Docker (local dev):**
```yaml
# memory-service/docker-compose.yml
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    volumes: ["redis_data:/data"]
    command: redis-server --save 60 1   # persist to disk every 60s
```

**Upgrade path:** Change the connection string to a managed Redis (AWS ElastiCache, Upstash) — zero code change.

---

#### 4.3.2 Long-Term Memory

**Definition:** Knowledge that persists across sessions and restarts — user preferences, project context, summarized conversation history, AGENTS.md memory files, cross-session facts.

**Implementation: Postgres via Docker → Cloud Postgres later**

Two things go in Postgres:

**A. LangGraph Checkpoint Archive (conversation history)**

DeepAgents' `SummarizationMiddleware` offloads old messages to `/conversation_history/{thread_id}.md` in the backend. For long-term, we store these in Postgres instead:

```python
# memory-service/src/long_term.py

from langgraph.checkpoint.postgres import PostgresSaver   # pip: langgraph-checkpoint-postgres

def get_long_term_store():
    return PostgresSaver.from_conn_string(
        "postgresql://user:pass@localhost:5432/yuyutsava"
    )
```

**B. Agent Memory Store (AGENTS.md / semantic facts)**

DeepAgents' `MemoryMiddleware` loads from a `StoreBackend` which wraps LangGraph's `BaseStore`. We back this with Postgres:

```python
# memory-service/src/long_term.py

from langgraph.store.postgres import PostgresStore

def get_memory_store():
    return PostgresStore.from_conn_string(
        "postgresql://user:pass@localhost:5432/yuyutsava"
    )
# Namespaced by user_id + assistant_id for isolation
```

**Docker (local dev):**
```yaml
# memory-service/docker-compose.yml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: yuyutsava
      POSTGRES_PASSWORD: localdev
      POSTGRES_DB: yuyutsava
    ports: ["5432:5432"]
    volumes: ["pg_data:/var/lib/postgresql/data"]
```

**Upgrade path:** Change `POSTGRES_URL` env var to point to AWS RDS, Supabase, Neon, or any cloud Postgres — zero code change.

---

#### 4.3.3 MongoDB (Optional Alternative for Long-Term)

If structured document storage is preferred over relational:

```yaml
# memory-service/docker-compose.yml (add alongside or instead)
services:
  mongodb:
    image: mongo:7
    ports: ["27017:27017"]
    volumes: ["mongo_data:/data/db"]
    environment:
      MONGO_INITDB_DATABASE: yuyutsava
```

Use case split:
- **Postgres**: LangGraph checkpoints, structured session metadata, user accounts
- **MongoDB**: Agent memory documents, AGENTS.md files, unstructured knowledge base

Decision can be deferred — the `StoreBackend` interface in DeepAgents is pluggable.

---

#### 4.3.4 CompositeBackend — Routing Short vs Long-Term

DeepAgents provides `CompositeBackend` which routes file paths to different backends:

```python
# memory-service/src/composite.py

from deepagents.backends import CompositeBackend, StateBackend, StoreBackend

def build_composite_backend(runtime, store):
    return CompositeBackend(
        default=StateBackend(runtime),           # Short-term: in-session state
        routes={
            "/memories/": StoreBackend(          # Long-term: Postgres-backed
                store=store,
                namespace=lambda ctx: ("memories", ctx.runtime.context.user_id)
            ),
            "/conversation_history/": StoreBackend(
                store=store,
                namespace=lambda ctx: ("history", ctx.runtime.context.thread_id)
            ),
        }
    )
```

This means:
- Anything under `/memories/` → persisted to Postgres `StoreBackend`
- Anything under `/conversation_history/` → persisted to Postgres
- Everything else (workspace files, temp files) → ephemeral `StateBackend`

---

### 4.4 Durable Execution

**Problem:** Agent tasks can take minutes. If the server restarts, or a network blip occurs, the work is lost and the agent starts from scratch.

**Solution:** LangGraph's built-in checkpointing, which DeepAgents already supports via the `checkpointer` parameter in `create_deep_agent()`.

**How it works:**
1. Every graph node execution, LangGraph saves the full `AgentState` to the checkpointer
2. If the process crashes mid-task, the state snapshot is in Redis (short-term) or Postgres (long-term)
3. On resume: invoke the graph with the same `thread_id` — LangGraph loads the last checkpoint and continues from exactly where it stopped

**Resume Pattern:**

```python
# task-runner/src/durable.py

async def resume_or_start(thread_id: str, task: str | None, agent, checkpointer):
    config = {"configurable": {"thread_id": thread_id}}
    
    # Check if there's an existing checkpoint
    checkpoint = await checkpointer.aget(config)
    
    if checkpoint and task is None:
        # Resume from last saved state — no new input needed
        result = await agent.ainvoke(None, config=config)
    else:
        # New task or extending existing thread
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=task)]},
            config=config
        )
    
    return result
```

**Thread ID Strategy:**
- Each session has one `thread_id` (UUID) generated at session creation
- Stored in the session record in Postgres/Redis
- Passed as `config["configurable"]["thread_id"]` on every invocation
- This is the single durable anchor — the same ID connects all checkpoints, history, and memory

---

### 4.5 Async Event Listener

**How agent events flow to the UI:**

```
agent.astream()                    (task-runner)
    │ yields AgentState updates
    ▼
Redis Pub/Sub channel              (task-runner publishes)
    │  channel: "task:{task_id}"
    ▼
API Gateway subscribes             (api-gateway)
    │ async generator
    ▼
SSE stream to client               (api-gateway → browser/app)
```

**Why Redis Pub/Sub and not direct WebSocket from worker:**
- The `task-runner` and `api-gateway` can run as separate processes/containers
- Redis decouples them — the API doesn't need to know which worker is running the task
- Scales horizontally: multiple workers, multiple API instances, all sharing one Redis

**Event Payload (per streamed update):**

```json
{
  "task_id": "abc123",
  "thread_id": "uuid-...",
  "event_type": "tool_call | tool_result | ai_message | done | error",
  "data": { ... },
  "timestamp": "2026-04-11T10:00:00Z"
}
```

---

## 5. `infra/` — Full Local Stack with Docker Compose

One command to bring up the entire v2.0 stack locally:

```yaml
# infra/docker-compose.yml

version: "3.9"
services:

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    volumes: ["redis_data:/data"]

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: yuyutsava
      POSTGRES_PASSWORD: localdev
      POSTGRES_DB: yuyutsava
    ports: ["5432:5432"]
    volumes: ["pg_data:/var/lib/postgresql/data"]

  api-gateway:
    build: ../api-gateway
    ports: ["8000:8000"]
    env_file: .env
    depends_on: [redis, postgres]

  task-runner:
    build: ../task-runner
    env_file: .env
    depends_on: [redis, postgres]
    volumes:
      - ../Backend:/app/Backend   # mount existing YUYUTSAVA CLI

volumes:
  redis_data:
  pg_data:
```

Run with: `docker compose -f infra/docker-compose.yml up`

---

## 6. Development Phases

### Phase 1 — Foundation (Start Here)

**Goal:** Get the agent runnable via API with basic session management.

Tasks:
1. Create `api-gateway/` with FastAPI
   - `POST /sessions` → returns `session_id` + `thread_id`
   - `POST /sessions/{id}/tasks` → queues task, returns `task_id`
   - `GET /sessions/{id}/tasks/{task_id}/stream` → SSE stream
2. Create `task-runner/` with a simple `asyncio` worker
   - Consume from asyncio queue (no Redis yet)
   - Call `build_agent()` from `Backend/yuyutsava/core/engine.py`
   - Use LangGraph `MemorySaver` (in-memory checkpointer) for now
3. Set up `infra/docker-compose.yml` with just the API + worker containers
4. Verify: Submit a task via `curl`, watch it stream back

**No Redis, no Postgres yet. Validate the wiring first.**

---

### Phase 2 — Short-Term Memory (Redis)

**Goal:** Sessions survive API restarts; concurrent sessions don't interfere.

Tasks:
1. Add Redis container to `infra/docker-compose.yml`
2. Create `memory-service/src/short_term.py` with `RedisSaver`
3. Wire `RedisSaver` as the checkpointer in `task-runner/src/invoker.py`
4. Implement Redis Pub/Sub for event streaming (replace asyncio queue)
5. Add session TTL (24 hours default, configurable)
6. Verify: Kill the task-runner mid-task, restart it, task resumes

---

### Phase 3 — Long-Term Memory (Postgres)

**Goal:** Memory persists across sessions; agent "remembers" past context.

Tasks:
1. Add Postgres container to `infra/docker-compose.yml`
2. Create `memory-service/src/long_term.py` with `PostgresSaver` + `PostgresStore`
3. Create `memory-service/src/composite.py` with `CompositeBackend` routing
   - `/memories/` → `StoreBackend` (Postgres)
   - `/conversation_history/` → `StoreBackend` (Postgres)
   - Everything else → `StateBackend` (ephemeral)
4. Wire `MemoryMiddleware` to load from `/memories/{user_id}/AGENTS.md`
5. Add Alembic migrations for Postgres schema
6. Verify: Agent references past session context in new session

---

### Phase 4 — Durable Execution

**Goal:** Agent tasks survive worker crashes and can be explicitly resumed.

Tasks:
1. Implement `task-runner/src/durable.py` with resume logic
2. Add `GET /sessions/{id}/tasks/{task_id}/resume` endpoint in API
3. Store task state (pending / running / completed / failed / resumable) in Redis
4. On worker startup, scan for "running" tasks from the dead worker and mark as "resumable"
5. Verify: SIGKILL the worker during a long task; resume via API; agent continues from checkpoint

---

### Phase 5 — UI Integration

**Goal:** Connect the frontend to the API.

Tasks:
1. Update `frontend/` to call `api-gateway` instead of CLI
2. Implement session management UI (list sessions, create, view history)
3. Connect SSE stream to real-time output display
4. Add voice input processing (external speech-to-text → text → POST /tasks)
5. File upload endpoint in `api-gateway` → writes to agent workspace

---

### Phase 6 — Hardening

**Goal:** Production-ready.

Tasks:
1. API key auth middleware in `api-gateway`
2. Rate limiting per session/user
3. Proper logging (structured JSON logs)
4. Health checks for all services
5. Replace Docker Redis/Postgres with cloud equivalents (env var swap)
6. Load test: 10+ concurrent sessions

---

## 7. Key Technology Decisions Summary

| Concern | Choice | Why |
|---------|--------|-----|
| API framework | FastAPI | Already in requirements; async-first; SSE support |
| Short-term memory | Redis (`langgraph-checkpoint-redis`) | Sub-ms latency, TTL, pub/sub, horizontally scalable |
| Long-term memory | Postgres (`langgraph-checkpoint-postgres` + `PostgresStore`) | Relational, queryable, cloud-portable |
| Durable execution | LangGraph checkpointing via `thread_id` | Native to DeepAgents; zero extra code in agent |
| Event streaming | Redis Pub/Sub → SSE | Decoupled worker/API; no direct socket needed |
| Local infra | Docker Compose | Matches existing Docker sandbox pattern in v1.0 |
| Package manager | `uv` | Already used in Backend |
| Agent core | Unchanged `Backend/yuyutsava/` | No breaking changes to v1.0 |

---

## 8. What Does NOT Change

- `Backend/yuyutsava/core/engine.py` — no modifications
- `Backend/yuyutsava/cli/cli.py` — no modifications  
- `Backend/yuyutsava/core/config.py` — no modifications
- The CLI `yuyutsava` command continues to work as before
- `DeepAgents` middleware stack — used as-is; `CompositeBackend`, `MemoryMiddleware`, `SummarizationMiddleware` are composed not rewritten

The v2.0 modules are **wrappers and extensions**, not replacements.

---

## 9. Open Questions (To Resolve Before Each Phase)

1. **Auth strategy**: API key only, or add OAuth (the frontend already has OAuth code)?
2. **MongoDB vs Postgres**: Do we need document storage, or is Postgres enough for long-term memory?
3. **Workspace isolation**: Should each session get its own Docker sandbox, or share a local workspace?
4. **Multi-user**: Is v2.0 single-user (local tool) or multi-tenant (hosted service)?
5. **Voice input**: Which speech-to-text service? (Groq Whisper, OpenAI Whisper, browser Web Speech API?)

---

## 10. First Steps Right Now

1. `cd /Users/abhinav0087/Desktop/YUYUTSAVA`
2. `mkdir api-gateway task-runner memory-service infra`
3. Start with Phase 1: `api-gateway/src/main.py` + `task-runner/src/worker.py`
4. Install `langgraph-checkpoint-redis` and `langgraph-checkpoint-postgres` in the new modules (not in Backend/)
5. Keep all new code on branch `yuyutsava_v2.0`
