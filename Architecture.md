# YUYUTSAVA Architecture

## Overview

YUYUTSAVA is an AI agent CLI that executes natural language tasks using file I/O and shell tools. It is built on **Deep Agents** (LangGraph-based), supports **Groq** and **OpenRouter** LLM providers, and can run tools either on the local host or inside an isolated **Docker sandbox**.

---

## CLI Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              yuyutsava [task]                                   │
│                           CLI Entry Point: cli.py                               │
└──────────────────────────────────┬──────────────────────────────────────────────┘
                                   │
                                   ▼
                          ┌────────────────┐
                          │  load_dotenv() │  ← reads .env file
                          └───────┬────────┘
                                  │
                                  ▼
              ┌───────────────────────────────────────┐
              │          Parse CLI Arguments           │
              │  argparse → args.task / args.scenario  │
              └───────────────┬───────────────────────┘
                              │
              ┌───────────────┼─────────────────────────┐
              │               │                         │
              ▼               ▼                         ▼
    ┌──────────────┐  ┌──────────────┐      ┌──────────────────────┐
    │--list-       │  │--print-tools │      │--generate_agent_graph│
    │  scenarios   │  │              │      │                      │
    └──────┬───────┘  └──────┬───────┘      └──────────┬───────────┘
           │                 │                          │
           ▼                 ▼                          ▼
    Print scenarios    Print tool JSON          Build agent graph
    and exit (0)       and exit (0)             → export PNG (Mermaid.Ink)
                                                → save State_Graph_v{n}.png
                                                → exit (0)

                              │ (normal task run)
                              ▼
              ┌───────────────────────────────────────┐
              │          Resolve Task Text             │
              │  --scenario → get_scenario().prompt    │
              │  positional  → " ".join(args.task)     │
              └───────────────┬───────────────────────┘
                              │
                              ▼
              ┌───────────────────────────────────────┐
              │       llm_settings_from_env()          │
              │                                        │
              │   LLM_PROVIDER=groq  ──► GroqSettings  │
              │   LLM_PROVIDER=openrouter ► OpenRouter  │
              └───────────────┬───────────────────────┘
                              │
                              ▼
              ┌───────────────────────────────────────┐
              │        Resolve Execution Mode          │
              │  --execution local|docker              │
              │  fallback: YUYUTSAVA_EXECUTION env var │
              └───────────────┬───────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              │                               │
              ▼                               ▼
    ┌──────────────────┐           ┌──────────────────────┐
    │   local mode     │           │    docker mode        │
    │                  │           │                       │
    │ LocalShellBackend│           │ DockerSandboxBackend  │
    │ (host filesystem │           │ - pulls docker image  │
    │  + host shell)   │           │ - mounts workspace    │
    │                  │           │ - optional /output    │
    │                  │           │ - sets network mode   │
    └────────┬─────────┘           └──────────┬────────────┘
             │                                │
             └──────────────┬─────────────────┘
                            │
                            ▼
              ┌───────────────────────────────────────┐
              │           build_agent()                │
              │                                        │
              │  chat_model(settings)                  │
              │  + backend                             │
              │  + system_prompt                       │
              │  → create_deep_agent(...)              │
              │  → AgentBundle(agent, docker_backend)  │
              └───────────────┬───────────────────────┘
                              │
                              ▼
              ┌───────────────────────────────────────┐
              │           invoke_agent()               │
              │                                        │
              │  agent.invoke({                        │
              │    "messages": [HumanMessage(task)]    │
              │  }, recursion_limit=N)                 │
              │                                        │
              │  ──► Agent Decision Loop (see below)   │
              │                                        │
              │  ◄── result["messages"]                │
              └───────────────┬───────────────────────┘
                              │
                              ▼
              ┌───────────────────────────────────────┐
              │  --verbose? → print message history    │
              │               to stderr                │
              │                                        │
              │  --docker-pull-paths? → docker cp      │
              │               paths out to host        │
              └───────────────┬───────────────────────┘
                              │
                              ▼
              ┌───────────────────────────────────────┐
              │     last_assistant_text(messages)      │
              │     → print final response to stdout   │
              └───────────────┬───────────────────────┘
                              │
                              ▼
                    bundle.close()
                    (stop Docker container if any)
                    exit(0)
```

---

## Agent Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                       Deep Agent Graph (LangGraph)                              │
│                         create_deep_agent(model, backend, system_prompt)        │
└──────────────────────────────────┬──────────────────────────────────────────────┘
                                   │
              ┌────────────────────┴────────────────────────┐
              │                                             │
              │  State: { "messages": [ ... ] }             │
              │                                             │
              └────────────────────┬────────────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │      START               │
                    │  HumanMessage(task)       │
                    │  appended to messages     │
                    └─────────────┬────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                         AGENT NODE                              │
│                                                                 │
│  Input: system_prompt + tool_schemas + messages history         │
│                                                                 │
│  LLM (Groq / OpenRouter via ChatOpenAI)                         │
│   model: llama-3.3-70b-versatile  OR  openai/gpt-4o-mini       │
│   temperature: 0.1    max_tokens: 4096                         │
│                                                                 │
│  Output → AIMessage with one of:                                │
│    (a) tool_calls: [{ name, args }]   → wants to use a tool     │
│    (b) content: "final answer text"   → task complete           │
└────────────────────────┬────────────────────────────────────────┘
                         │
              ┌──────────┴──────────┐
              │  tool_calls?        │
              │                     │
         YES  ▼               NO   ▼
┌─────────────────────┐    ┌────────────────────────┐
│  TOOL EXECUTOR NODE │    │    END                 │
│                     │    │                        │
│  Routes each call   │    │  last_assistant_text() │
│  to backend:        │    │  → return to caller    │
│                     │    └────────────────────────┘
│  ┌───────────────┐  │
│  │ read_file     │  │
│  │ write_file    │  │
│  │ edit_file     │  │
│  │ execute       │  │     ◄── LocalShellBackend
│  │ ls            │  │         (host filesystem + shell)
│  │ glob          │  │
│  │ grep          │  │     OR
│  │ write_todos   │  │
│  │ task          │  │         DockerSandboxBackend
│  └───────────────┘  │         (docker exec inside container)
│                     │
│  → ToolMessage      │
│    (output, exit)   │
└──────────┬──────────┘
           │
           │  append ToolMessage to messages
           │
           ▼
  ┌─────────────────────────────────────────────────────┐
  │             Recursion Limit Check                    │
  │                                                      │
  │  iterations < recursion_limit (default: 200) ?       │
  │                                                      │
  │  YES → loop back to AGENT NODE                       │
  │  NO  → raise RecursionError (safety guard)           │
  └─────────────────────────────────────────────────────┘
           │
           │  YES
           ▼
   ┌────────────────────┐
   │    AGENT NODE      │  ← (next iteration)
   │  (same as above,   │
   │   with updated     │
   │   message history) │
   └────────────────────┘
```

### Agent State Transitions

```
  ┌──────┐     HumanMessage(task)      ┌────────────┐
  │START │ ──────────────────────────► │ agent_node │
  └──────┘                             └─────┬──────┘
                                             │
                          ┌──────────────────┴──────────────────┐
                          │                                      │
                          ▼  tool_calls present                  ▼  no tool_calls
                  ┌───────────────┐                        ┌──────────┐
                  │  tool_node    │                        │   END    │
                  └───────┬───────┘                        └──────────┘
                          │  ToolMessage(s)
                          ▼
                   ┌────────────┐
                   │ agent_node │  ← loop
                   └────────────┘
```

---

## Backend Architecture

```
                        ┌─────────────────────────────────┐
                        │        AgentBundle               │
                        │                                  │
                        │  agent: CompiledStateGraph        │
                        │  docker_backend: Optional[Docker] │
                        │  close(): stop container          │
                        └────────────┬────────────────────┘
                                     │
                     ┌───────────────┴───────────────┐
                     │                               │
                     ▼                               ▼
        ┌────────────────────────┐    ┌──────────────────────────────┐
        │   LocalShellBackend    │    │     DockerSandboxBackend      │
        │   (deepagents built-in)│    │     (docker_sandbox_backend.py)│
        │                        │    │                              │
        │  root_dir: workspace   │    │  image: deepagent-sandbox    │
        │  virtual_mode: True    │    │  workspace → /workspace      │
        │  timeout: bash_timeout │    │  export_dir → /output        │
        │  inherit_env: True     │    │  network: bridge|none        │
        │                        │    │                              │
        │  Path translation:     │    │  Container lifecycle:        │
        │  /foo → workspace/foo  │    │  __init__ → docker run -d    │
        │                        │    │  execute  → docker exec -i   │
        │  execute():            │    │  stop()   → docker kill      │
        │  subprocess on host    │    │                              │
        │  cwd = workspace_root  │    │  Path translation:           │
        └────────────────────────┘    │  /foo → /workspace/foo       │
                                      │  /output → export_host/      │
                                      └──────────────────────────────┘
```

---

## LLM Provider Configuration

```
                    ┌─────────────────────────┐
                    │   llm_settings_from_env()│
                    │   config.py              │
                    └──────────┬──────────────┘
                               │
              ┌────────────────┴─────────────────┐
              │  LLM_PROVIDER env var             │
              │                                  │
         "groq"▼                  "openrouter"   ▼
  ┌────────────────────┐     ┌──────────────────────────┐
  │   GroqSettings     │     │   OpenRouterSettings      │
  │                    │     │                           │
  │  api_key:          │     │  api_key:                 │
  │   GROQ_API_KEY     │     │   OPENROUTER_API_KEY      │
  │  base_url:         │     │  base_url:                │
  │   GROQ_BASE_URL    │     │   OPENROUTER_BASE_URL     │
  │  model:            │     │  model:                   │
  │   llama-3.3-70b-   │     │   openai/gpt-4o-mini      │
  │   versatile        │     │  http_referer / x_title   │
  └─────────┬──────────┘     └────────────┬─────────────┘
            │                             │
            └──────────────┬──────────────┘
                           │  LlmSettings Protocol
                           ▼
              ┌─────────────────────────┐
              │      chat_model()       │
              │      llm.py             │
              │                         │
              │  ChatOpenAI(            │
              │    api_key=...,         │
              │    base_url=...,        │
              │    model=...,           │
              │    temperature=0.1,     │
              │    max_tokens=4096,     │
              │    default_headers=...  │
              │  )                      │
              └─────────────────────────┘
```

---

## Module Dependency Map

```
  cli.py
  ├── scenarios.py          (built-in demo prompts)
  ├── config.py             (LlmSettings, llm_settings_from_env)
  ├── engine.py             (build_agent, invoke_agent, export_agent_state_graph_png)
  │   ├── config.py         (LlmSettings Protocol)
  │   ├── llm.py            (chat_model → ChatOpenAI)
  │   ├── docker_sandbox_backend.py  (DockerSandboxBackend)
  │   └── deepagents         (create_deep_agent, LocalShellBackend)  [external]
  │       └── langgraph      (CompiledStateGraph)                    [external]
  └── docker_sandbox_backend.py  (pull_virtual_paths_to_host)
```

---

## Key Design Decisions

| Concern | Decision |
|---|---|
| LLM provider | Protocol-based abstraction (`LlmSettings`) — swap Groq/OpenRouter without code changes |
| Backend abstraction | Factory pattern for `LocalShellBackend`; direct instance for `DockerSandboxBackend` |
| Path safety | Virtual path scoping in both backends prevents workspace escape |
| Loop safety | LangGraph `recursion_limit` (default 200) stops runaway tool-call loops |
| Container reuse | Docker container kept alive with `sleep infinity` across multiple tool calls |
| Observability | `--verbose` streams full message history (Human/AI/Tool) to stderr; stdout stays clean |
| Execution isolation | `--execution docker` runs all shell commands inside an ephemeral container |

---

---

# Daemon + Electron UI Architecture

## Overview

The daemon mode adds an **always-on background process** that watches your environment and routes events through an AI agent pipeline. The system has two runtime components:

- **Backend**: Python FastAPI server (the _daemon_) — runs all agent logic, bound to loopback only
- **Frontend**: Electron app (the _UI_) — surfaces agent activity and collects user decisions

They communicate over **localhost HTTP** (`127.0.0.1:7654` by default).

---

## Full System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         ELECTRON APP (Frontend)                         │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  Renderer Process (React + Vite)                                 │  │
│  │                                                                  │  │
│  │   ┌──────────────┐  ┌─────────────────┐  ┌────────────────┐    │  │
│  │   │  Proposals   │  │  ActivityLog    │  │   Settings     │    │  │
│  │   │   Panel      │  │  (SSE feed)     │  │   Panel        │    │  │
│  │   │  (proposals  │  │  (log/token/    │  │  (env vars +   │    │  │
│  │   │   + asks)    │  │   tool events)  │  │   daemon ctrl) │    │  │
│  │   └──────┬───────┘  └───────┬─────────┘  └────────────────┘    │  │
│  │          │                  │                                    │  │
│  │          └──────────────────┼───────────────────────────────    │  │
│  │                             │                                    │  │
│  │                  ┌──────────▼──────────┐                        │  │
│  │                  │    useSSE() hook     │ ◄── SSE stream         │  │
│  │                  │   (React Context)    │     (proposals, asks,  │  │
│  │                  │                      │      log, token, etc.) │  │
│  │                  └──────────┬──────────┘                        │  │
│  │                             │                                    │  │
│  │                  ┌──────────▼──────────┐                        │  │
│  │                  │     SSEClient        │──► GET /stream         │  │
│  │                  │   (api/sse.js)       │──► POST /proposal/:id  │  │
│  │                  │                      │──► POST /ask/:id       │  │
│  │                  │                      │──► GET /rules          │  │
│  │                  │                      │──► GET /decisions      │  │
│  │                  └──────────────────────┘                        │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                              │ Electron IPC                             │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  Main Process (Node.js)                                          │  │
│  │                                                                  │  │
│  │   ┌───────────────────┐   ┌─────────────────────────────────┐  │  │
│  │   │    daemon.js      │   │       ipc-handlers.js           │  │  │
│  │   │  spawn / manage   │   │  daemon:start/stop/restart/port  │  │  │
│  │   │  Python process   │   │  settings:get/save              │  │  │
│  │   │  ping /health     │   │  tray:badge, notify:show        │  │  │
│  │   └────────┬──────────┘   └─────────────────────────────────┘  │  │
│  └────────────┼────────────────────────────────────────────────────┘  │
└───────────────┼─────────────────────────────────────────────────────────┘
                │ spawn: uv run yuyutsava daemon --no-ui --workspace <cwd>
                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        PYTHON DAEMON (Backend)                          │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  FastAPI App  (uvicorn on 127.0.0.1:7654)                        │  │
│  │                                                                  │  │
│  │  GET  /stream              SSE push stream (live events)         │  │
│  │  POST /proposal/:id/respond  Tier-1 user consent decision        │  │
│  │  POST /ask/:id/respond       Tier-2 tool permission answer       │  │
│  │  GET  /rules                 List saved consent rules            │  │
│  │  DELETE /rules/:id           Revoke a consent rule               │  │
│  │  GET  /decisions             Decision timeline (last N)          │  │
│  │  GET  /skills                List available agent skills         │  │
│  │  DELETE /skills/:name        Delete a personal skill             │  │
│  │  GET  /health                Liveness check (Electron ping)      │  │
│  └───────────────────────────────────┬────────────────────────────┘  │
│                                      │                                  │
│                             ┌────────▼────────┐                        │
│                             │    WebHub        │                        │
│                             │  - SSE queue     │                        │
│                             │  - pending_proposals: {id → Future}      │
│                             │  - pending_asks:     {id → Future}       │
│                             └────────┬────────┘                        │
│                                      │                                  │
│                             ┌────────▼────────┐                        │
│                             │   WebChannel    │ (UserChannel impl)      │
│                             │  post_event()   │ → broadcast to SSE     │
│                             │  post_proposal()│ → SSE + await Future   │
│                             │  post_ask()     │ → SSE + await Future   │
│                             └────────┬────────┘                        │
│                                      │                                  │
│                             ┌────────▼────────┐                        │
│                             │  ChannelRouter  │                        │
│                             │  fan-out events │                        │
│                             │  route asks to  │                        │
│                             │  primary channel│                        │
│                             └────────┬────────┘                        │
│                                      │                                  │
│              ┌───────────────────────┼─────────────────────┐          │
│              ▼                       ▼                       ▼          │
│       ┌────────────┐         ┌──────────────┐    ┌──────────────────┐ │
│       │TriageLoop  │         │Orchestrator  │    │   SubAgents      │ │
│       │            │         │    Loop      │    │ FileOrganizer    │ │
│       │ LLM triage │         │ LangGraph    │    │ FaceWatcher      │ │
│       │ event →    │         │ graph run    │    │                  │ │
│       │ task_queue │         │ streams to   │    │ TaskRunnerAgent  │ │
│       └─────┬──────┘         │ WebChannel   │    │  (shell tools)   │ │
│             │                └──────────────┘    └──────────────────┘ │
│             │ OrchestratorTask                                          │
│      ┌──────▼────────┐                                                 │
│      │   EventBus    │                                                 │
│      └──────┬────────┘                                                 │
│             │                                                           │
│  ┌──────────▼──────────────────────────────────────┐                 │
│  │              Event Sources                       │                 │
│  │  filesystem │ webcam │ voice │ clipboard │ hotkey│                 │
│  └─────────────────────────────────────────────────┘                 │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Communication Protocols

### 1. Electron Main → Python Daemon (Process Management)

The Electron **main process** spawns and manages the daemon as a child process:

```
Electron Main Process (daemon.js)
        │
        │  spawn('uv', ['run', 'yuyutsava', 'daemon', '--no-ui', ...])
        │  env: { ...process.env, ...settings }
        ▼
Python Daemon Process
        │
        │  HTTP GET http://127.0.0.1:7654/health
        ▼
Readiness detected → renderer can connect to /stream

Shutdown:
  SIGTERM → wait 3s → SIGKILL
```

### 2. Electron Renderer ↔ Electron Main (Electron IPC)

```
React Renderer (window.electronAPI)         Electron Main (ipc-handlers.js)
────────────────────────────────            ──────────────────────────────
  .getDaemonPort()          ──────────────► ipcMain.handle('daemon:port')
  .getDaemonStatus()        ──────────────► ipcMain.handle('daemon:status')
  .startDaemon()            ──────────────► ipcMain.handle('daemon:start')
  .stopDaemon()             ──────────────► ipcMain.handle('daemon:stop')
  .restartDaemon()          ──────────────► ipcMain.handle('daemon:restart')
  .getSettings()            ──────────────► ipcMain.handle('settings:get')
  .saveSettings()           ──────────────► ipcMain.handle('settings:save')
  .setProposalCount(n)      ──────────────► ipcMain.on('tray:badge', n)
  .showNotification(opts)   ──────────────► ipcMain.on('notify:show', opts)
  .onNotificationClick(cb)  ◄────────────── main forwards OS click event
```

The `preload.js` exposes a safe `window.electronAPI` bridge; the renderer has no direct Node.js access.

### 3. React UI ↔ FastAPI (SSE + REST — The Core Loop)

```
React Renderer                                FastAPI Daemon
─────────────────                             ─────────────

SSEClient.connect()
  EventSource → GET /stream ────────────────────────────────────────►
                                        WebHub.subscribe() holds connection open

                ◄──── event: hello {ts} ──────────────────────────────
                ◄──── event: event  {kind:"log", data:{text}} ─────────
                ◄──── event: event  {kind:"token", data:{token}} ──────
                ◄──── event: event  {kind:"tool_call", data:{name,args}}
                ◄──── event: event  {kind:"timeline", data:{...}} ─────
                ◄──── event: proposal {proposal_id, title, body, ...} ─
                ◄──── event: ask {ask_id, title, body, options} ────────

User clicks Approve on a Tier-1 proposal:
  POST /proposal/:id/respond ──────────────────────────────────────►
    body: { decision: "approve" | "approve_remember" | "skip" | ... }
                              store.try_set_proposal_status(...)
                              pending_proposals[id].set_result(decision)
                              Orchestrator unblocks, continues task ◄──
                ◄──── HTTP 200 {ok: true} ────────────────────────────

User answers a Tier-2 permission ask:
  POST /ask/:id/respond ───────────────────────────────────────────►
    body: { response: "approve" | "reject" }
                              pending_asks[id].set_result(response)
                              TaskRunner tool call resumes ◄──────────
                ◄──── HTTP 200 {ok: true} ────────────────────────────
```

---

## SSE Event Types

The `/stream` endpoint uses Server-Sent Events. Each message has an `event:` type and JSON `data:`:

| SSE `event:` | Payload shape | UI consumer |
|---|---|---|
| `hello` | `{ts}` | Connection indicator in Titlebar |
| `event` | `{kind, data}` | ActivityLog — kinds: `log`, `token`, `tool_call`, `tool_result`, `timeline` |
| `proposal` | `{proposal: {proposal_id, title, body, expires_ts, ...}}` | ProposalsPanel — awaits user click |
| `ask` | `{ask: {ask_id, title, body, options}}` | ProposalsPanel — awaits user click |

---

## Two-Tier Consent System

```
─────────────────────────────── TIER 1: Proposals ──────────────────────────────

Agent intends to perform an action  (rename files, send message, etc.)
        │
        │  TriageLoop.post_proposal(p)
        ▼
WebChannel.post_proposal(p)
  1. Creates asyncio.Future
  2. Broadcasts SSE event: proposal
  3. await Future (blocks until user responds or timeout)
        │
        │  SSE → UI ProposalsPanel card appears
        │  User clicks: Approve / Skip / Modify / Approve-remember
        │  UI: POST /proposal/:id/respond {decision}
        │
  4. Future.set_result(ProposalDecision)
  5. Orchestrator unblocks → executes or skips
        │
        ▼  decision: approve | approve_remember | modify | skip | skip_remember | expired

─────────────────────────────── TIER 2: Asks ──────────────────────────────────

SubAgent tool (tr_write_file, tr_execute, etc.) needs mid-run permission
        │
        │  TaskRunnerAgent raises interrupt
        ▼
OrchestratorLoop.ask_handler(interrupt_value)
  1. Creates asyncio.Future
  2. Broadcasts SSE event: ask
  3. await Future (blocks tool call)
        │
        │  SSE → UI permission card appears
        │  User picks: approve / reject
        │  UI: POST /ask/:id/respond {response}
        │
  4. Future.set_result(response)
  5. LangGraph resumes, tool executes or is cancelled
```

---

## Agent Pipeline: Events → Actions

```
Event Sources
(file watch / webcam / clipboard / voice / hotkey)
        │
        │  raw event pushed to EventBus
        ▼
┌────────────────┐
│  TriageLoop    │  LLM reads event, decides:
│                │   - ignore (no action)
│                │   - route to subagent + task description
└───────┬────────┘
        │  OrchestratorTask → task_queue
        ▼
┌────────────────────────┐
│   OrchestratorLoop     │  pops task, builds LangGraph graph per task
│                        │  streams tokens → post_event(token) → SSE
│                        │  tool calls   → post_event(tool_call) → SSE
│                        │  permission needed → post_ask() → blocks
│                        │  action proposed  → post_proposal() → blocks
└───────────┬────────────┘
            │
            ▼
┌───────────────────────────────┐
│  SubAgents (via LangGraph)    │
│                               │
│  FileOrganizerAgent           │  moves/organises files in Downloads etc.
│  FaceWatcherAgent             │  processes webcam frames, face events
│                               │
│  └─► TaskRunnerAgent          │  executes shell commands (with permission gates)
│          tr_read_file         │
│          tr_write_file        │  → Tier-2 ask before executing
│          tr_execute           │
│          tr_glob / tr_grep    │
└───────────────────────────────┘
```

---

## Startup Sequence

```
1. Electron app starts
2. Main process reads settings.json (port, API keys, model names)
3. daemon.js pings GET /health on configured port
4.   ↳ not reachable → spawn Python:
         uv run yuyutsava daemon --no-ui --workspace <cwd>
         with settings as env vars
5. Python daemon boots:
     a. load .env (dotenv)
     b. DaemonConfig, EventsConfig from file/env
     c. Store (SQLite) starts
     d. MCPClientManager starts (optional MCP tool servers)
     e. CheckpointerManager (SQLite, for LangGraph state)
     f. EventBus + SourceRegistry (filesystem watchers etc.) start
     g. WebHub + WebChannel created
     h. TerminalChannel added (always present, fallback)
     i. TriageAgent, TaskRunnerAgent, SubAgents constructed
     j. uvicorn starts on 127.0.0.1:7654
     k. TriageLoop + OrchestratorLoop start as asyncio tasks
6. Electron renderer loads React app
7. useSSE() hook opens EventSource to GET /stream
8. Server sends: event: hello → UI shows green "Connected" indicator
9. Daemon processes events; all updates stream via SSE in real time
```

---

## Shutdown Sequence

```
SIGTERM / SIGINT received
        │
        ▼
stop_event.set()
        │
        ├─ registry.stop_all()       (stop filesystem watchers etc.)
        ├─ bus.close()               (wake triage loop's async-for)
        │
        ├─ TriageLoop drains         (finishes current triage decision)
        ├─ OrchestratorLoop drains   (finishes in-flight agent task, 10s timeout)
        ├─ uvicorn stops             (closes SSE connections)
        │
        ├─ channels.shutdown()
        ├─ mcp_manager.stop()
        ├─ blob_sweeper.stop()
        ├─ checkpointer_mgr.stop()
        └─ store.stop()              (closes SQLite)
```

---

## FastAPI Endpoints Reference

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/stream` | SSE push stream — live events, proposals, asks |
| `POST` | `/proposal/{id}/respond` | User decision on a Tier-1 proposal |
| `POST` | `/ask/{id}/respond` | User answer on a Tier-2 tool permission |
| `GET` | `/rules` | List saved consent rules |
| `DELETE` | `/rules/{id}` | Revoke a consent rule |
| `GET` | `/decisions` | Recent decision timeline (default last 50) |
| `GET` | `/skills` | List bundled + personal agent skills |
| `DELETE` | `/skills/{name}` | Delete a personal-scope skill |
| `GET` | `/health` | Liveness check used by Electron ping |
| `GET` | `/` | Serve static `index.html` (standalone mode) |
| `GET` | `/static/{file}` | Serve JS/CSS assets |

---

## Security Design

- FastAPI **refuses to bind** to non-loopback addresses at startup — network exposure is structurally impossible
- CORS is locked to `http://localhost` and `http://127.0.0.1` with any port
- No auth tokens — security is purely network-level (loopback only, single-user machine)
- `/docs` and `/redoc` are disabled (`docs_url=None, redoc_url=None`)
- Blob sweeper auto-deletes webcam frames after 1 hour (privacy by default)
