# Deep Agents tutorial — architecture and flow

This document describes how the **`goog`** CLI, shared tutorial code, and **Deep Agents** (`create_deep_agent`) fit together: entry points, runtime flow, and tools the agent can use.

Official overview: [LangChain Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview).

---

## 1. System architecture (components)

```mermaid
flowchart TB
    subgraph User["User / operator"]
        U[Natural language task or scenario id]
    end

    subgraph CLI["goog CLI (cli.py)"]
        P[Parse args]
        BR{{Branch}}
        LS[--list-scenarios]
        PT[--print-tools]
        RUN[Run agent]
    end

    subgraph Config["tutorials/shared/config.py"]
        ENV[".env + LLM_PROVIDER"]
        GROQ[GroqSettings]
        OR[OpenRouterSettings]
    end

    subgraph Model["tutorials/shared/groq_chat.py"]
        CHAT[ChatOpenAI]
    end

    subgraph Deep["tutorials/shared/deep_tutorial.py"]
        BLD[build_tutorial_deep_agent]
        BE["LocalShellBackend factory\nvirtual_mode=True, root_dir=workspace"]
        SYS[System prompt: workspace root + tool rules]
        INV[invoke_tutorial_agent]
    end

    subgraph Lib["deepagents + langgraph"]
        DAG[create_deep_agent → CompiledStateGraph]
        TOOLS["Built-in tools:\nread_file, write_file, execute,\nls, glob, grep, edit_file,\nwrite_todos, task, …"]
    end

    subgraph Disk["Host"]
        WS[("-w workspace (real disk + shell cwd)")]
    end

    U --> P
    P --> BR
    BR --> LS
    BR --> PT
    BR --> RUN
    RUN --> ENV --> GROQ
    RUN --> ENV --> OR
    GROQ --> CHAT
    OR --> CHAT
    RUN --> BLD
    CHAT --> BLD
    BE --> BLD
    SYS --> BLD
    BLD --> DAG
    DAG --> TOOLS
    TOOLS --> WS
    RUN --> INV
    INV --> DAG
    INV --> OUT[Final assistant text → stdout / stderr if -v]
```

---

## 2. End-to-end run flow (one invocation)

```mermaid
flowchart LR
    A[load_dotenv] --> B{tutorial_llm_settings_from_env}
    B --> C[resolve workspace -w]
    C --> D[build_tutorial_deep_agent]
    D --> E[invoke_tutorial_agent]
    E --> F["agent.invoke({ messages: [HumanMessage(task)] }, recursion_limit)"]
    F --> G{LangGraph loop}
    G -->|model decides| H[AIMessage + optional tool_calls]
    H --> I[ToolMessage results from backend]
    I --> G
    G -->|done| J[last_assistant_text]
    J --> K{verbose?}
    K -->|yes| L[stderr: Human / AI / tool trace]
    K -->|no| M[stdout: final answer only]
```

---

## 3. CLI entry modes (tasks vs utilities)

```mermaid
flowchart TD
    START([goog]) --> OPT{How was it invoked?}

    OPT -->|flag --list-scenarios| L1[Print scenario ids from scenarios.py]
    OPT -->|flag --print-tools| L2[Print builtin_tools_reference_json]
    OPT -->|flag --scenario with id| S[Load prompt from get_scenario]
    OPT -->|positional words only| T[Join argv into one task string]

    S --> CONFLICT{Any positional task words too?}
    CONFLICT -->|both scenario and task| ERR[stderr: do not mix --scenario with task text]
    CONFLICT -->|scenario only| RUN[Build agent + invoke]

    T --> NEED{Joined task is blank?}
    NEED -->|blank after strip| ERR2[stderr: need task, --scenario, or a list flag]
    NEED -->|non-empty string| RUN

    RUN --> DONE([exit 0])
    ERR --> E1([exit 2])
    ERR2 --> E1
    L1 --> DONE0([exit 0])
    L2 --> DONE0
```

---

## 4. Built-in tutorial scenarios (`--scenario`)

```mermaid
flowchart LR
    subgraph Scenarios["scenarios.py — pass scenario id to --scenario"]
        E1["explore_bash\nexecute: list workspace"]
        E2["read_then_summarize\nread_file README"]
        E3["write_artifact\nwrite_file from_agent.txt"]
        E4["full_loop\necho → read → write → summary"]
    end
```

| id | Intent |
| --- | --- |
| `explore_bash` | Use **execute** to list workspace; summarize |
| `read_then_summarize` | **read_file** on playground README; summarize |
| `write_artifact` | **write_file** a small artifact under workspace |
| `full_loop` | **execute** → **read_file** → **write_file** → short summary |

---

## 5. Agent actions (tools)

```mermaid
flowchart TB
    AGENT[Deep Agent LLM]

    subgraph FS["Filesystem (virtual paths under workspace)"]
        RF[read_file]
        WF[write_file]
        EF[edit_file]
        LS[ls]
        GL[glob]
        GR[grep]
    end

    subgraph SH["Host shell"]
        EX[execute]
    end

    subgraph META["Planning / delegation (Deep Agents defaults)"]
        WT[write_todos]
        TK[task subagent]
        MORE["… per deepagents docs"]
    end

    AGENT --> RF & WF & EF & LS & GL & GR
    AGENT --> EX
    AGENT --> WT & TK & MORE
```

This tutorial configures **`LocalShellBackend`**: file tools map under **`-w`**, and **`execute`** runs shell on the host (trusted environments only). See `README.md` for env vars and security notes.

---

## 6. Sequence: one reasoning loop

```mermaid
sequenceDiagram
    participant U as User
    participant CLI as goog CLI
    participant G as CompiledStateGraph
    participant M as Chat model
    participant B as LocalShellBackend

    U->>CLI: task or --scenario
    CLI->>G: invoke HumanMessage
    loop Until final answer
        G->>M: messages + tool schemas
        alt tool_calls
            M-->>G: AIMessage with tool_calls
            G->>B: read_file / write_file / execute / …
            B-->>G: ToolMessage
        else text only
            M-->>G: AIMessage (answer)
        end
    end
    G-->>CLI: final messages
    CLI-->>U: stdout final text (or stderr trace if -v)
```
