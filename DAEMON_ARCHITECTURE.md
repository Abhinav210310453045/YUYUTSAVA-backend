# Yuyutsava Daemon — Architecture & Flow Diagrams

---

## 1. System Overview — Component Map

```mermaid
graph TB
    subgraph OS["OS / Environment"]
        FS[("Filesystem\nDownloads, Desktop, etc.")]
        ENV[".env / events_config.json"]
    end

    subgraph Sources["Event Sources Layer"]
        SR[SourceRegistry]
        FSS[FsSource]
        SR --> FSS
    end

    subgraph EventLayer["Event Infrastructure"]
        BUS[EventBus\npub/sub]
        STORE[("Store\nSQLite\n~/.yuyutsava/state.db")]
    end

    subgraph Processing["Processing Layer"]
        TL[TriageLoop]
        OL[OrchestratorLoop]
    end

    subgraph Agents["Agent Layer"]
        ORCH[OrchestratorGraph\nLangGraph]
        TR[TaskRunnerAgent]
        FO[FileOrganizerAgent]
    end

    subgraph Channels["User Channel Layer"]
        CR[ChannelRouter]
        WC[WebChannel\nFastAPI + SSE]
        TC[TerminalChannel\nstderr]
    end

    subgraph LLM["LLM Providers"]
        TLLM[Triage LLM]
        OLLM[Orchestrator LLM]
        SLLM[SubAgent LLM]
    end

    FS -->|inotify/FSEvents| FSS
    ENV --> SR
    FSS -->|ctx.emit| BUS
    FSS -->|put_event_payload| STORE
    BUS -->|subscribe all topics| TL
    TL -->|classify| TLLM
    TL -->|put_proposal\nput_decision\nput_consent_rule| STORE
    TL -->|post_proposal| CR
    CR --> WC
    CR --> TC
    WC -->|SSE /stream| Browser["Browser / UI"]
    Browser -->|POST /proposal/id/respond| WC
    TL -->|enqueue OrchestratorTask| OL
    OL -->|build_orchestrator| ORCH
    ORCH -->|invoke subagents| TR
    ORCH -->|invoke subagents| FO
    ORCH -->|LLM calls| OLLM
    TR -->|LLM calls| SLLM
    FO -->|LLM calls| SLLM
    OL -->|stream events| CR
    OL -->|put_decision| STORE
```

---

## 2. Daemon Boot Sequence

```mermaid
sequenceDiagram
    participant Main as main.py
    participant Env as .env / Config
    participant Store as Store (SQLite)
    participant Bus as EventBus
    participant SR as SourceRegistry
    participant CR as ChannelRouter
    participant LLM as LLM factories
    participant TL as TriageLoop
    participant OL as OrchestratorLoop
    participant Web as WebServer (Uvicorn)

    Main->>Env: load_dotenv()
    Main->>Env: build DaemonConfig + EventsConfig
    Main->>Store: await store.start() → WAL pragma, writer task
    Main->>Bus: EventBus()
    Main->>SR: SourceRegistry(bus, store, events_config)
    Main->>SR: await registry.start_all()
    Note over SR: spawns per-source tasks\nwith backoff supervision
    Main->>CR: ChannelRouter([WebChannel, TerminalChannel])
    Main->>LLM: llm_settings_from_env("triage")
    Main->>LLM: llm_settings_from_env("orchestrator")
    Main->>LLM: llm_settings_from_env("subagent")
    Main->>TL: TriageLoop(bus, store, router, triage_model)
    Main->>OL: OrchestratorLoop(store, router, orch_model, subagents)
    Main->>Web: uvicorn.serve(app, host, port)
    Note over Main: asyncio.wait([triage_task, orch_task, web_task],\nreturn_when=FIRST_COMPLETED)
    Note over Main: On SIGINT/SIGTERM → graceful shutdown
```

---

## 3. Graceful Shutdown Sequence

```mermaid
sequenceDiagram
    participant OS as OS (SIGINT/SIGTERM)
    participant Main as main.py
    participant SR as SourceRegistry
    participant Bus as EventBus
    participant TL as TriageLoop
    participant OL as OrchestratorLoop
    participant CR as ChannelRouter
    participant Store as Store

    OS->>Main: signal received
    Main->>Main: stop_event.set()
    Main->>SR: await registry.stop_all()
    Note over SR: sets cancelled event\nawaits source.stop()\ncancels source tasks
    Main->>Bus: bus.close()
    Note over Bus: sends None sentinel\nto all subscriber queues\nwakes TriageLoop async-for
    Bus-->>TL: None sentinel → loop exits
    Main->>OL: await drain in-flight tasks (10s timeout)
    Main->>CR: await router.shutdown()
    Main->>Store: await store.stop()
    Note over Store: writer task drains queue\ncloses SQLite connection
```

---

## 4. Event Ingestion — Source to Bus

```mermaid
flowchart TD
    A[OS Filesystem Event\ninotify / FSEvents / kqueue] --> B[FsSource._watch_loop]
    B --> C{event type?}
    C -->|created / modified| D[ctx.emit\ntopic=fs.changed\nhints=ext, kind\nseverity=1]
    C -->|deleted| E[ctx.emit\ntopic=fs.deleted\nseverity=1]
    C -->|other| F[ignore]

    D --> G[SourceContext.emit]
    E --> G

    G --> H[store.put_event_payload\nevent_id, topic, ts,\npayload_json, blob_path]
    G --> I[EventEnvelope\nevent_id=ULID\ntopic=dotted string\nsource=fs\nts=epoch\nseverity=int\nsummary max 120 chars\npayload_ref=sqlite ref\nhints=dict]
    I --> J[bus.publish\nevent_id]

    J --> K{fan-out to subscribers}
    K -->|pattern match fnmatch| L[TriageLoop queue\nbounded 256]
    K -->|pattern match fnmatch| M[other subscribers\nif any]

    L --> N{queue full?}
    N -->|yes| O[log drop, not blocking]
    N -->|no| P[deliver envelope]
```

---

## 5. Triage Loop — Full Event Handling Flow

```mermaid
flowchart TD
    START([TriageLoop.run]) --> SUB[subscribe bus pattern **]
    SUB --> WAIT[async for envelope in subscription]
    WAIT --> SEM{semaphore\nmax 4 concurrent}
    SEM -->|acquired| TASK[asyncio.create_task\n_handle envelope]
    SEM -->|full| BACKPRESSURE[wait for slot]

    TASK --> CONSENT{check consent_rules\nfrom store}
    CONSENT -->|topic glob + hints match\ndecision=auto_skip| SKIP_LOG[log skip decision\nput_decision store]
    CONSENT -->|topic glob + hints match\ndecision=auto_approve| AUTO[_auto_approve_path]
    CONSENT -->|no rule matches| CLASSIFY[triage.classify\nev + capabilities_block]

    CLASSIFY --> TRIAGE_LLM[Triage LLM\nreturns TriageDecision\naction, subagent_hint,\nurgency, summary,\ninstruction]

    TRIAGE_LLM --> ACTION{action?}
    ACTION -->|drop| DROP[log drop\nput_decision store]
    ACTION -->|log| LOG_ONLY[put_decision store\nno task created]
    ACTION -->|propose| PROPOSE[create Proposal\nput_proposal store]

    PROPOSE --> CHANNEL[router.post_proposal\nProposal → UI]
    CHANNEL --> WAIT_USER[await ProposalDecision\nfrom user]

    WAIT_USER --> USER_DEC{decision?}
    USER_DEC -->|approve / approve_remember| ENQUEUE[enqueue OrchestratorTask]
    USER_DEC -->|skip / skip_remember| RECORD_SKIP[put_decision store]
    USER_DEC -->|modify| MOD_ENQUEUE[use modified instruction\nenqueue OrchestratorTask]
    USER_DEC -->|expired| EXPIRE[put_decision store]

    ENQUEUE --> STORE_DEC[put_decision store\noutcome=approved]

    AUTO --> AUTO_APPROVE[create minimal TriageDecision\nmark proposal approved\nput_proposal + put_decision]
    AUTO_APPROVE --> ENQUEUE

    USER_DEC -->|approve_remember\nor skip_remember| RULE[_add_consent_rule_for\nauto_approve or auto_skip\n7-day expiry]
    RULE --> ENQUEUE

    ENQUEUE --> QUEUE[OrchestratorTask\ninto asyncio.Queue]

    SKIP_LOG --> WAIT
    DROP --> WAIT
    LOG_ONLY --> WAIT
    RECORD_SKIP --> WAIT
    EXPIRE --> WAIT
    QUEUE --> WAIT
```

---

## 6. Orchestrator Loop — Task Execution

```mermaid
flowchart TD
    START([OrchestratorLoop.run]) --> GET[await task_queue.get\ntimeout=1s]
    GET -->|timeout| START
    GET -->|OrchestratorTask| THREAD[generate thread_id\norch-uuid4]

    THREAD --> BUILD[build_orchestrator\nmodel, deps, budget_tokens]
    BUILD --> GRAPH[LangGraph CompiledStateGraph\nwith BudgetMiddleware]

    GRAPH --> RENDER[task.render_to_message\nformatted prompt string]
    RENDER --> STREAM[astream_agent_iter\nagent, task_msg, thread_id]

    STREAM --> ITER{stream events}
    ITER -->|StreamEvent token| BROADCAST_T[router.post_event\nChannelEvent token]
    ITER -->|StreamEvent tool_call| BROADCAST_TC[router.post_event\nChannelEvent tool_call]
    ITER -->|StreamEvent tool_result| BROADCAST_TR[router.post_event\nChannelEvent tool_result]
    ITER -->|StreamEvent log| BROADCAST_L[router.post_event\nChannelEvent log]
    ITER -->|StreamEvent final| FINAL[capture final text]
    ITER -->|interrupt ask| ASK[router.post_ask\nAskPrompt → await user answer]
    ASK -->|answer| RESUME[resume agent with answer]
    RESUME --> ITER

    FINAL --> SAVE[store.put_decision\noutcome=completed\naction_summary=final text]
    SAVE --> TIMELINE[router.post_event\nChannelEvent timeline]
    TIMELINE --> START
```

---

## 7. Core Engine — `astream_agent_iter` Internals

```mermaid
sequenceDiagram
    participant OL as OrchestratorLoop
    participant Engine as core/engine.py
    participant Graph as LangGraph Agent
    participant BM as BudgetMiddleware
    participant LLM as LLM Provider
    participant Tools as SubAgent Tools

    OL->>Engine: astream_agent_iter(agent, task, thread_id)
    Engine->>Graph: astream(input, config, stream_mode=["messages","updates"])

    loop each graph chunk
        Graph->>BM: check token budget
        BM-->>Graph: inject SystemMessage "wrap up" if over cap

        alt AIMessageChunk (token)
            Graph-->>Engine: chunk → StreamEvent("token", data)
            Engine-->>OL: yield StreamEvent token
        end

        alt tool_call update
            Graph->>Tools: invoke tool
            Tools-->>Graph: tool result guarded max 100k chars
            Graph-->>Engine: StreamEvent("tool_call") + StreamEvent("tool_result")
            Engine-->>OL: yield both events
        end

        alt __interrupt__ (Tier-2 permission)
            Graph-->>Engine: interrupt value
            Engine->>OL: yield StreamEvent("log", interrupt)
            OL->>Router: router.post_ask(AskPrompt)
            Router-->>OL: user answer string
            OL->>Engine: send answer via ask_handler callback
            Engine->>Graph: graph.astream(Command(resume=answer))
        end
    end

    Engine-->>OL: yield StreamEvent("final", text)
```

---

## 8. SubAgent Execution — Inside the Orchestrator Graph

```mermaid
flowchart TD
    ORCH[OrchestratorGraph\nLangGraph ReAct] --> DECIDE{which subagent?}

    DECIDE -->|subagent_hint=task_runner\nor file ops needed| TR_INVOKE[invoke TaskRunnerAgent]
    DECIDE -->|subagent_hint=file_organizer\nor organize files| FO_INVOKE[invoke FileOrganizerAgent]

    TR_INVOKE --> TR_GRAPH[TaskRunnerAgent\nbuild_react_agent\nwith own LLM + tools]
    FO_INVOKE --> FO_GRAPH[FileOrganizerAgent\nbuild_react_agent\nwith own LLM + tools]

    TR_GRAPH --> TR_TOOLS{tools}
    TR_TOOLS --> TR_READ[tr_read\nread file contents]
    TR_TOOLS --> TR_WRITE[tr_write\nwrite file]
    TR_TOOLS --> TR_DELETE[tr_delete\ndelete file]
    TR_TOOLS --> TR_EXEC[tr_execute_in_sandbox\nrun shell command]
    TR_TOOLS --> TR_GREP[tr_grep\nsearch files]

    FO_GRAPH --> FO_TOOLS{tools}
    FO_TOOLS --> FO_READ[read file]
    FO_TOOLS --> FO_MOVE[move / rename file]
    FO_TOOLS --> FO_LIST[list directory]

    TR_GRAPH --> BM_TR[BudgetMiddleware\nsubagent_token_budget\n30k tokens]
    FO_GRAPH --> BM_FO[BudgetMiddleware\nsubagent_token_budget\n30k tokens]

    BM_TR --> RESULT_TR[result → back to OrchestratorGraph]
    BM_FO --> RESULT_FO[result → back to OrchestratorGraph]
```

---

## 9. Channel Routing & User Communication

```mermaid
flowchart TD
    SOURCE{event origin}
    SOURCE -->|OrchestratorLoop token/tool| CE[ChannelEvent\nkind: token/tool_call/tool_result/log/timeline]
    SOURCE -->|TriageLoop proposal| PROP[Proposal\nproposal_id, summary,\ninstruction, urgency]
    SOURCE -->|OrchestratorLoop interrupt| ASK[AskPrompt\nask_id, question]

    CE --> ROUTER[ChannelRouter]
    PROP --> ROUTER
    ASK --> ROUTER

    ROUTER -->|post_event\ngather all| WC[WebChannel]
    ROUTER -->|post_event\ngather all| TC[TerminalChannel\nstderr]

    ROUTER -->|post_proposal\ntry primary first| WC
    WC -->|no response| TC

    ROUTER -->|post_ask\ntry primary first| WC
    WC -->|no response| TC

    WC -->|SSE /stream| BROWSER[Browser]
    WC -->|pending future| HUB[WebHub\ndict proposal_id→Future]
    BROWSER -->|POST /proposal/id/respond| WC_API[FastAPI endpoint]
    WC_API --> HUB
    HUB -->|resolve future| WC
    WC -->|ProposalDecision| ROUTER
    ROUTER -->|ProposalDecision| TL[TriageLoop\nawaiting decision]

    TC -->|print proposal| STDERR[stderr / TTY]
    STDERR -->|user types| TC_INPUT[TerminalChannel input]
    TC_INPUT -->|ProposalDecision| TL
```

---

## 10. SQLite Store — Data Model & Access Patterns

```mermaid
erDiagram
    EVENT_PAYLOADS {
        string event_id PK
        string topic
        float ts
        text payload_json
        string blob_path
    }
    PROPOSALS {
        string proposal_id PK
        string event_id FK
        string topic
        text summary
        text proposed
        string subagent
        int urgency
        float created_ts
        float expires_ts
        string status
    }
    DECISIONS {
        string decision_id PK
        string proposal_id FK
        string event_id FK
        string outcome
        text action_summary
        float ts
    }
    CONSENT_RULES {
        string rule_id PK
        string topic_glob
        text match_json
        string decision
        float created_ts
        float expires_ts
    }

    EVENT_PAYLOADS ||--o{ PROPOSALS : "event_id"
    PROPOSALS ||--o{ DECISIONS : "proposal_id"
```

```mermaid
flowchart LR
    subgraph Writes["Async Writes (queued)"]
        W1[put_event_payload] -->|FsSource via ctx.emit| STORE
        W2[put_proposal] -->|TriageLoop| STORE
        W3[put_decision] -->|TriageLoop + OrchestratorLoop| STORE
        W4[put_consent_rule] -->|TriageLoop on approve/skip_remember| STORE
    end

    subgraph Reads["Sync Reads (asyncio thread)"]
        R1[list_consent_rules] -->|TriageLoop._handle\non every event| STORE
        R2[get_event_payload] -->|OrchestratorGraph recall tool| STORE
        R3[list_decisions] -->|GET /decisions endpoint| STORE
        R4[recall topic_glob + since_sec] -->|OrchestratorGraph| STORE
        R5[try_set_proposal_status CAS] -->|WebChannel POST handler| STORE
    end

    STORE[("SQLite\nstate.db\nWAL mode")]
```

---

## 11. Consent Rule Matching — Triage Decision Tree

```mermaid
flowchart TD
    EV[EventEnvelope arrives] --> RULES[load list_consent_rules\nfrom store]
    RULES --> MATCH{iterate rules\nfind first match}

    MATCH -->|rule.topic_glob fnmatch topic| HINT{hints predicates\nmatch_json?}
    HINT -->|all predicates match\nor match_json empty| FOUND{rule.decision}
    HINT -->|mismatch| NEXT[next rule]
    NEXT --> MATCH

    FOUND -->|auto_skip| SKIP[log + put_decision\noutcome=auto_skip\nno LLM call]
    FOUND -->|auto_approve| APPROVE[_auto_approve_path\nno LLM call\nno user prompt]

    MATCH -->|no rule matched| LLM_CLASSIFY[triage.classify\nLLM call]
    LLM_CLASSIFY --> DECISION{TriageDecision.action}

    DECISION -->|drop| DROP_OUT[put_decision drop]
    DECISION -->|log| LOG_OUT[put_decision log_only]
    DECISION -->|propose| PROPOSAL[Proposal → user]

    PROPOSAL --> USER{user responds}
    USER -->|approve_remember| NEW_APPROVE_RULE[put_consent_rule\ndecision=auto_approve\n7-day TTL]
    USER -->|skip_remember| NEW_SKIP_RULE[put_consent_rule\ndecision=auto_skip\nunlimited TTL]
    NEW_APPROVE_RULE --> TASK[OrchestratorTask enqueued]
    NEW_SKIP_RULE --> SKIP_FUTURE[future events auto-skipped]
    USER -->|approve| TASK
    USER -->|skip| SKIP_OUT[put_decision skipped]
    USER -->|modify| MOD[modified instruction\nOrchestratorTask enqueued]
```

---

## 12. Token Budget Enforcement

```mermaid
flowchart TD
    MSG[LLM invocation in graph] --> BM[BudgetMiddleware\nbefore_invoke]
    BM --> CHECK{spent_tokens at cap?}
    CHECK -->|no| PASS[pass messages to LLM unchanged]
    CHECK -->|yes| INJECT[inject SystemMessage:\nYou have N tokens left, wrap up]
    INJECT --> LLM[LLM call]
    PASS --> LLM

    LLM --> RESPONSE[LLM response]
    RESPONSE --> TRACK[BudgetMiddleware.after_invoke\nspent += usage_metadata.input_tokens]
    TRACK --> NEXT[next graph node]

    subgraph Budgets["Budget Caps per Role"]
        B1[orchestrator_token_budget\ndefault 8000 tokens]
        B2[subagent_token_budget\ndefault 30000 tokens]
    end
```

---

## 13. Source Registry — Supervision & Backoff

```mermaid
flowchart TD
    START([registry.start_all]) --> IMPORT[_import_builtin_sources\nimport yuyutsava.events.sources.fs]
    IMPORT --> ITER[for each source in events_config.sources\nwhere enabled=true]
    ITER --> TASK[asyncio.create_task\n_run_source_with_backoff]

    subgraph BACKOFF["_run_source_with_backoff loop"]
        TRY[source.start ctx] --> CRASH{exception?}
        CRASH -->|no| DONE[source exited cleanly]
        CRASH -->|yes| COUNT{failures under 5?}
        COUNT -->|yes| WAIT[exponential backoff\npow 2 failures seconds]
        WAIT --> TRY
        COUNT -->|no| QUARANTINE[log quarantine\nstop retrying]
    end

    TASK --> BACKOFF

    STOP([registry.stop_all]) --> CANCEL[set cancelled asyncio.Event\nper source]
    CANCEL --> AWAIT[await source.stop]
    AWAIT --> TASKS[cancel source tasks\nawait them]
```

---

## 14. End-to-End Flow — File Downloaded to Task Completed

```mermaid
sequenceDiagram
    participant FS as Filesystem
    participant FsSrc as FsSource
    participant Bus as EventBus
    participant Store as SQLite Store
    participant TL as TriageLoop
    participant TriLLM as Triage LLM
    participant Router as ChannelRouter
    participant UI as Browser / UI
    participant OL as OrchestratorLoop
    participant Orch as OrchestratorGraph
    participant SA as TaskRunnerAgent
    participant OrchLLM as Orchestrator LLM
    participant SALLM as SubAgent LLM

    FS->>FsSrc: file created in ~/Downloads/report.pdf
    FsSrc->>Store: put_event_payload(event_id, topic=fs.changed, payload)
    FsSrc->>Bus: publish(EventEnvelope fs.changed hints={ext=pdf})

    Bus->>TL: deliver envelope (fanout)
    TL->>Store: list_consent_rules()
    Store-->>TL: [rules list]
    TL->>TL: no matching rule
    TL->>TriLLM: classify(envelope, capabilities)
    TriLLM-->>TL: TriageDecision: action=propose, subagent_hint=task_runner, urgency=2
    TL->>Store: put_proposal(proposal_id, ...)
    TL->>Router: post_proposal(Proposal)
    Router->>UI: SSE proposal event

    UI->>Router: POST /proposal/{id}/respond {decision: approve_remember}
    Router->>TL: ProposalDecision(approve_remember)
    TL->>Store: put_consent_rule(auto_approve, topic_glob=fs.changed, hints={ext=pdf})
    TL->>Store: put_decision(outcome=approved)
    TL->>OL: enqueue OrchestratorTask

    OL->>Orch: build_orchestrator + stream
    Orch->>OrchLLM: plan task
    OrchLLM-->>Orch: invoke TaskRunnerAgent with file path
    Orch->>SA: TaskRunnerAgent.run
    SA->>SALLM: reason about file
    SALLM-->>SA: call tr_read(report.pdf)
    SA->>Store: (blob read if needed)
    SA-->>Orch: summary text
    Orch->>OrchLLM: finalize response
    OrchLLM-->>Orch: final summary

    Orch-->>OL: StreamEvent(final, text)
    OL->>Store: put_decision(outcome=completed, action_summary)
    OL->>Router: post_event(ChannelEvent timeline)
    Router->>UI: SSE timeline event
```

---

## 15. Key Invariants & Design Principles

```mermaid
mindmap
  root((Yuyutsava Daemon))
    Async Throughout
      All I/O non-blocking via asyncio
      Single writer task for SQLite
      Concurrent reads on asyncio thread
    Consent Tiers
      Tier-1 Proposal
        LLM classifies event
        User approves or skips
        Rules learned for future
      Tier-2 Tool Permission
        In-flight agent interrupt
        router.post_ask blocks
        User approves tool call
    Event-Driven Decoupling
      Sources never touch bus directly
      ctx.emit is the only interface
      Bus fanout is non-blocking
      Drop policy on full queues
    Token Safety
      BudgetMiddleware per role
      Hard cap injects wrap-up message
      Orchestrator 8k / SubAgent 30k
    Graceful Shutdown
      Stop sources first
      Close bus wakes triage loop
      Drain in-flight 10s timeout
      Shutdown channels last
    Extensibility ABCs
      EventSource
      UserChannel
      BaseSubAgent
```
