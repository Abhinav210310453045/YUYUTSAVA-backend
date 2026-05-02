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
