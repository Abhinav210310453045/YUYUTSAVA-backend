# Yuyutsava Daemon — Architecture & Flow Diagrams

> **Scope.** Diagram-first companion to [`Architecture.md`](overview.md),
> focused on the **daemon's runtime flows**. Architecture.md is the prose
> reference and covers both operating modes; this file is the picture book for
> boot, event ingestion, triage, orchestration and shutdown. For the wire-level
> view of the SSE/WebSocket surfaces drawn below — frame catalogs, `seq`/replay
> semantics, the voice PCM path — see [`docs/Transport.md`](transport.md).
>
> **Updated** after the Phase 0–4 architecture remediation
> (`docs/architecture/review/`). The structural changes that show up here:
> `build_daemon` split into seven builders, the fourteen middleware classes
> replaced by one `LangChainPolicyAdapter` over plain policies, the two stream
> drivers merged into `_drive_graph` + two sinks, and the SQLite/Postgres store
> twins collapsed onto one `Dialect` adapter.

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
        STORE[("Store — one impl per domain\nDialect: SQLite | Postgres+pgvector")]
    end

    subgraph Processing["Processing Layer"]
        TL[TriageLoop]
        OL[OrchestratorLoop]
    end

    subgraph Agents["Agent Layer"]
        ORCH[OrchestratorGraph\nLangGraph]
        POL[LangChainPolicyAdapter\n14 policies, 1 middleware]
        TR[TaskRunnerAgent\npermission gateway]
        GP[general-purpose\nfile-organizer · face-watcher]
    end

    subgraph Async["Background Subagents"]
        HOST[AsyncSubagentHost\nin-proc LangGraph server]
        MIRROR[AsyncTaskMirror]
    end

    subgraph Channels["User Channel Layer"]
        CR[ChannelRouter]
        WC[WebChannel\nFastAPI + SSE + WS]
        TC[TerminalChannel\nstderr]
        VC[VoiceChannel]
        TG[Telegram plugin]
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
    CR --> VC
    CR --> TG
    WC -->|SSE /stream · WS /ws/converse| Browser["Electron UI / mobile"]
    Browser -->|POST /proposal/id/respond| WC
    TL -->|enqueue OrchestratorTask| OL
    OL -->|build_orchestrator| ORCH
    ORCH --> POL
    POL -->|policy-gated| TR
    POL -->|policy-gated| GP
    ORCH -. start_async_task .-> HOST --> GP
    HOST --> MIRROR --> ORCH
    ORCH -->|LLM calls| OLLM
    TR -->|LLM calls| SLLM
    GP -->|LLM calls| SLLM
    OL -->|stream events| CR
    OL -->|put_decision| STORE
```

---

## 2. Daemon Boot Sequence

`main.py` **runs**; `bootstrap.build_daemon()` **wires**. The wiring used to be one
927-line function; ADR-003 split it into seven named builders, each independently
testable and under 200 lines.

```mermaid
sequenceDiagram
    participant Main as daemon/main.py
    participant Boot as bootstrap.build_daemon()
    participant BS as build_storage
    participant BP as build_policy
    participant BR as build_retention
    participant BE as build_events
    participant BSA as build_subagents
    participant BA as build_async_subagents
    participant Loops as asyncio tasks

    Main->>Main: acquire_daemon_lock() (singleton per user)
    Main->>Boot: await build_daemon(opts)

    Boot->>BS: backend (sqlite | postgres + migrations v1–v20)
    Note over BS: StoreFactory makes the backend choice ONCE\nfor CLI, tinker and daemon alike
    BS-->>Boot: StorageSubsystem (stores · embedder · registry · usage)

    Boot->>BP: permissions.json · consent registry · cap enforcer
    Boot->>BR: MCP manager · checkpointer · unified TTL sweeper
    Boot->>BE: EventBus · SourceRegistry.start_all() · ChannelRouter
    Note over BE: +WebChannel if UI · +Terminal always · +Voice if --voice
    Boot->>BE: ResourceMonitor + AdmissionController · role models
    Boot->>BSA: subagents (general-purpose · file-organizer · face-watcher) + TaskRunner
    Boot->>BA: task_queue · LaunchIndex · [AsyncSubagentHost + Mirror + Watcher]

    Boot->>Boot: TriageAgent + TriageLoop · TaskSubmissionService
    Boot->>Boot: OrchestratorDeps + OrchestratorLoop · DecisionService
    Boot->>Boot: FastAPI app + uvicorn.Server (bearer auth iff non-loopback)
    Boot-->>Main: DaemonSubsystems (frozen record)

    Main->>Main: write_daemon_discovery(pid, web_url, async_host_url)
    Main->>Loops: triage · orchestrator · sweeper · resource-monitor · uvicorn · reload
    Main->>Loops: resume_interrupted_tasks() (durable resume from checkpoint)
    Note over Main: asyncio.wait(..., return_when=FIRST_COMPLETED)
```

> **A boot bug this shape hides.** An extraction once stranded an import inside a
> Postgres-only branch: every test suite stayed green and the daemon only failed
> on a live PG boot. `test/daemon/test_bootstrap_no_unbound_names.py` now catches
> that statically, along with the mirror-image case (a local import shadowing a
> module-level one), across `bootstrap.py` and `engine.py`.

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
    ACTION -->|drop| DROP[put_decision outcome=dropped\n+ timeline line\nsee note below]
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

> **`drop` used to be silent.** Every other outcome wrote a decision row and a
> timeline line; `drop` alone did `return`. For a `mode=triage` submission that
> meant the task registry row sat `queued` forever with **no evidence anywhere
> that triage had seen it** — indistinguishable, from the UI, from a broken
> daemon. Found by submitting one against a running daemon and watching nothing
> happen. It now records `outcome="dropped"` with the classifier's reason.
>
> The registry row still stays `queued` — that is deliberate v1 behaviour
> (`task_submission.submit_via_triage`), and whether a dropped task belongs in a
> terminal state is a product decision, not a bug fix.

---

## 6. Orchestrator Loop — Task Execution

```mermaid
flowchart TD
    START([OrchestratorLoop.run]) --> GET[await task_queue.get\ntimeout=1s]
    GET -->|timeout| START
    GET -->|OrchestratorTask| THREAD[generate thread_id\norch-uuid4]

    THREAD --> BUILD[build_orchestrator\nmodel, deps, budget_tokens]
    BUILD --> GRAPH[CompiledStateGraph\n+ LangChainPolicyAdapter\n14 policies incl. Budget]

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

## 7. Core Engine — the stream driver

`core/streaming.py` used to hold **two** ~226-line drivers that were ~90%
identical. ADR-004 collapsed them into one driver plus two sinks: `_drive_graph`
owns the loop, interrupt collection and the resume handshake; `astream_agent_iter`
turns its items into `StreamEvent`s and `astream_agent` prints them.

```mermaid
sequenceDiagram
    participant OL as OrchestratorLoop
    participant Sink as astream_agent_iter (sink)
    participant Drive as _drive_graph (the driver)
    participant Graph as Agent (ports.Agent)
    participant Pol as LangChainPolicyAdapter
    participant LLM as LLM Provider

    OL->>Sink: astream_agent_iter(agent, task, thread_id, ask_handler)
    Sink->>Drive: _drive_graph(agent, input, cfg, ask=ask_handler)
    Drive->>Graph: astream(input, config, stream_mode=["messages","updates"])

    loop each graph chunk
        Graph->>Pol: revise_model_call → prompt + tool list
        Pol-->>Graph: revised request
        Graph->>LLM: model call
        Pol-->>Graph: after_model → Budget may inject a wrap-up Directive

        alt AIMessageChunk
            Drive-->>Sink: ("chunk", (chunk, meta))
            Sink-->>OL: StreamEvent("token", {text, node, ns})
        end

        alt tool call / result
            Graph->>Pol: before_tool → Denied | Raw | proceed
            Pol-->>Graph: refusal ToolMessage, or the tool runs
            Graph->>Pol: after_tool → offload rewrites oversized results
            Drive-->>Sink: ("message", m)
            Sink-->>OL: StreamEvent("tool_call") + StreamEvent("tool_result")
        end

        alt __interrupt__
            Drive->>Drive: collect pending (id, value)
        end
    end

    Drive-->>Sink: ("pass_end", steps)
    alt pending interrupts
        Drive->>Sink: ask(value) per interrupt → channel-routed
        Sink-->>Drive: decision string
        Drive->>Graph: _resume_command(decisions)
    else none
        Sink-->>OL: StreamEvent("final", {text})
    end
```

**Why one driver mattered.** The multi-interrupt resume protocol was implemented
in both copies and they had drifted: only the daemon one handled interrupts
arriving with **no ids**; the CLI one built `Command(resume={})` and discarded
every answer the user had just typed. `_resume_command` is now the single
implementation.

`_drive_graph` is annotated against `ports.Agent` (`astream` + `aget_state`), not
`CompiledStateGraph` — so its whole parity suite runs against a scripted double
that is not a graph.

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

    TR_GRAPH --> BM_TR[LangChainPolicyAdapter\nToolFilter · Offload · Budget 30k · Usage]
    FO_GRAPH --> BM_FO[LangChainPolicyAdapter\nToolFilter · Offload · Budget 30k · Usage]

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

## 10. The Store — Data Model & Access Patterns

> **One implementation, two backends.** Each domain below used to have a
> hand-written SQLite store *and* a Postgres twin — 19 pairs that drifted
> silently. `storage/dialect.py` names the five things that actually differ
> (placeholder, timestamp, JSON, parent-row FK, vector support) and each domain
> now has one implementation over it. Timestamps are `TIMESTAMPTZ` on Postgres
> since migration v20; the events schema gained matching foreign keys in SQLite v5.

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

    subgraph Reads["Reads"]
        R1[list_consent_rules] -->|TriageLoop._handle\non every event| STORE
        R2[get_event_payload] -->|OrchestratorGraph recall tool| STORE
        R3[list_decisions] -->|GET /decisions endpoint| STORE
        R4[recall topic_glob + since_sec] -->|OrchestratorGraph| STORE
        R5[try_set_proposal_status CAS] -->|WebChannel POST handler| STORE
    end

    STORE[("One store per domain\nDialect: SQLite WAL | Postgres+pgvector")]
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

`BudgetPolicy` (was `BudgetMiddleware`) is a plain `Policy` — no framework import,
tested without a graph. It reads `Turn.usage`, which the adapter resolves **once**
per turn from the latest `AIMessage`; before ADR-004 the budget and the usage
recorder each dug that out of `usage_metadata` separately.

```mermaid
flowchart TD
    MSG[model call completes] --> AD[LangChainPolicyAdapter.aafter_model]
    AD --> RESOLVE[Turn.usage resolved once\nlatest AIMessage · dict or object]
    RESOLVE --> BP[BudgetPolicy.after_model]
    BP --> ACC[spent += usage.input_tokens]
    ACC --> CHECK{spent >= cap?}
    CHECK -->|no| NONE[return None — no state update]
    CHECK -->|already fired| NONE
    CHECK -->|yes, first time| DIR[return Directive\nStop calling tools. Summarise…]
    DIR --> INJECT[adapter converts to\nSystemMessage in state]

    RESOLVE --> UP[UsagePolicy.after_model]
    UP --> ROW[one llm_usage row\ntokens · model · role · task_id]
    ROW --> COST{model in price table?}
    COST -->|yes| EST[est_cost_usd]
    COST -->|no| WARN[est_cost_usd = 0.00\n+ warn ONCE naming the model]

    subgraph Budgets["Budget caps per role"]
        B1[orchestrator_token_budget\ndefault 8000 tokens]
        B2[subagent_token_budget\ndefault 30000 tokens]
    end
```

Two properties worth keeping:

- **The directive fires once.** It is a wrap-up instruction, not a hard stop —
  killing the graph mid-tool-call would leave an orphaned tool call in state,
  which is a worse failure than one more turn.
- **A call with no reported usage counts as nothing**, never as zero. A zero row
  is indistinguishable from a genuinely free call once the ledger is summed.

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
      BudgetPolicy per role
      Cap injects a wrap-up Directive, once
      Orchestrator 8k / SubAgent 30k
      Unpriced model warns, never guesses
    Graceful Shutdown
      Stop sources first
      Close bus wakes triage loop
      Drain in-flight 10s timeout
      Shutdown channels last
    Extensibility ABCs
      EventSource
      UserChannel
      BaseSubAgent
      Provider — the llm layer
    Our Own Abstractions
      Policy + one LangChainPolicyAdapter
      ports/ dependency-free Protocols
      Dialect: 2 backends, 1 implementation
      ModelHandle: identity from the provider
      AskUser: HITL without calling interrupt
    Nothing Fails Silently
      Triage drop is recorded
      Unpriced model warns once
      Filesystem-block strip warns when it matches nothing
      Storage degrade/recover posts a timeline notice
```
