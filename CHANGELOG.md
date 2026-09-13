# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).
While the version stays `0.x`, minor bumps may contain breaking changes.

## [Unreleased]

### Fixed
- **A conversation could go permanently silent.** Sending a message while a
  tool call was running made LangGraph cancel the call and fabricate a
  `status="success"` result saying "cancelled"; from then on every model call
  returned `finish_reason: STOP` with zero output tokens and the prompt cache
  dropped to nothing. Four messages, ~70,000 input tokens each, no reply and no
  error. The repair for this existed but ran only on CLI `--resume`; it now
  lives in `conversation/repair.py` and every turn — CLI, app, voice, and
  subagents, whose cancelled `task` calls land in the parent's history — runs
  it before sending. A turn that still produces nothing repairs, retries once,
  and then *says so*: `log` events may be dropped by a renderer with nowhere to
  put them (which is exactly what swallowed four warnings), so there is a new
  `notice` kind that may not be.
- **Typing during a turn no longer kills the tool call.** The message is
  queued and sent when the turn ends, with an explicit *send now* that
  interrupts — safe, because the next turn repairs the interrupted history.
  `Ctrl+S` in the CLI.
- **The chat showed only the final reply.** The renderer kept one bubble per
  turn and then let `final` replace everything in it, so prose → tool → prose
  collapsed into a blob and the reasoning between steps was lost. A tool call
  now closes the block, as the CLI has always done.
- **A question could not be answered from the chat.** `AskCard` sets
  `overflow: hidden`, which makes its flex minimum size 0, so in a long thread
  it was crushed to an invisible sliver and the only way to answer was the
  Inbox. Also, a second question silently replaced the first and left it
  blocking its agent — asks are a queue now.
- **Scrolling during a reply was impossible**: auto-scroll pinned to the bottom
  on every streaming frame. It now follows only when you are already there.
- **The split terminal view painted outside itself.** Long lines from
  `warnings.warn` and from `logging` bled through the context column, the
  banner was wiped by the alternate screen, log records landed on top of the
  prompt, PgUp/PgDn did nothing, and the panel truncated. Wrapping is now
  ANSI- and width-aware at render time, every writer is captured (not just the
  root logger), and the panel fits its height by priority.
- **The context meter reported `system prompt ≈0`** on every call, because it
  looked for a `SystemMessage` in state and the framework passes the prompt as
  `request.system_message`.

### Added
- **The Logs panel shows what the daemon is doing.** It used to contain only
  HTTP access lines — the UI's own polling — while ~500 log records across
  ~150 loggers went to stderr. A log bridge now forwards them as structured
  `app_log` events with level and subsystem, and the panel gained a filter, a
  minimum-level select and an HTTP toggle. The titlebar log-level dropdown
  finally affects what you can see.
- Transient asides (provider retries, an unpriced model) appear in the CLI's
  context column instead of printing over the transcript or the prompt.
- `docs/design/computer-use.md` — the accessibility-tree-first design for GUI
  control, with what is already installed and permitted, and the two genuine
  gaps. Design only; nothing is implemented.
- Context and cost telemetry, in both fronts. A context meter measures every
  model call — how full the input window is and what it is made of (system
  prompt, tool schemas, memory, skills, messages), plus the call's reported
  tokens and the conversation's running spend — and publishes it to whoever is
  displaying it. Previously a session filling its window looked exactly like
  one that was fine, and the numbers could only be reconstructed from the
  database afterwards.
  - **CLI:** `yuyutsava chat` opens a split view on a TTY 90+ columns wide —
    transcript left, a context column right that never scrolls away. It takes
    the alternate screen, so `--classic` (or `YUYUTSAVA_REPL_DASHBOARD=0`)
    keeps the single-pane transcript and a narrower terminal falls back
    automatically. PgUp/PgDn or the wheel scrolls, `End` follows, `Ctrl+G`
    hides the panel, `Ctrl+L` clears. `/context` prints the full breakdown.
  - **App:** a collapsible context column on the chat and voice screens (a
    one-line meter in the header, resizable panel behind it), and a new
    **Settings → Usage** section with totals, per-model bars, a per-day series
    and a per-session table across every session.
  - Segment sizes are estimates and are marked `≈` — a provider reports one
    total per prompt, not a figure per region — calibrated against each
    conversation's reported input tokens. Last-call figures are the provider's
    own numbers.
- `llm_usage` records `cache_read_tokens` and `cache_creation_tokens`. Cache
  reads are the dominant cost lever on a long conversation and nothing stored
  them, so a cheap session and an expensive one were indistinguishable in the
  ledger. Both are subsets of `input_tokens`, not additions to it.
- `GET /usage/summary` (totals, per-model and a per-day series for one range,
  in one call) and `GET /usage/sessions` (per-conversation spend, joined to
  session titles). `GET /usage` rows gained the cache columns, and
  `WS /ws/converse` carries `usage` frames while a turn runs.
- A model with no entry in `model_prices.json` now reads *unpriced* everywhere
  instead of `$0.00`; `/usage/summary` names which models those are, so a
  total that is an undercount says so.
- `ctx_history`, `ctx_history_message` and `ctx_history_grep`: page, read and
  search this conversation's verbatim messages. Compaction is the only step
  that removes messages from the live context, and until now the summary it
  left behind was the only trace — the originals were in the transcript store
  with no way to ask for them. The summary and its prompt now name the
  read-back path.
- Line-addressed artifact reads: `ctx_fetch_artifact(id, start_line=…,
  line_count=…)` beside the character form, so a `ctx_grep_artifact` hit can
  be widened directly.
- Token accounting for standalone CLI runs (previously daemon-only): each turn
  prints its calls, tokens and estimated cost, and `/usage` reports session
  totals by model. `/skills` shows which skills were recalled this turn and
  what else is available.
- Artifacts render inline in the terminal, inside a bounded box — markdown,
  text, code, csv and json drawn in place, html/jsx/audio announced with their
  path. Display-only: the body is read from disk, never added to the
  conversation.
- A macOS host-control skill pack (`macos-open-app-or-url`,
  `chrome-profile-by-email`, `whatsapp-mac-chat-db`, `macos-app-local-data`).
- Per-workspace state directory `<workspace>/.yuyutsava/` — the only place
  yuyutsava writes inside a workspace: `sandbox/`, `scripts/`, `outputs/`,
  `tmp/`, workspace `skills/`, the append-only `ChangeLog.md`,
  `Assumptions.md` and `workspace_memory.md`, and an optional workspace-level
  `mcp_config.json`. It carries its own `*` `.gitignore`. `WorkspaceLayout`
  (`storage/paths.py`) is the single source of truth for these paths.
- `tr_write_file(append=True)` for the append-only knowledge files, and a
  shared WORKSPACE STATE prompt block telling every agent what lives where and
  how to recall from it (`tr_grep` the state dir before starting).
- Workspace-level MCP config, merged with the global one and gated by a
  `trusted_workspaces` list; `${YUYUTSAVA_WORKSPACE}` and `$VAR` expand in
  `command`, `args`, `url` and `env`. The standalone CLI now starts MCP servers
  too — previously only the daemon did.
- Docker mode runs `tr_execute_in_sandbox` and `tr_run_python` inside the
  container; before, the container was started but every tool ran on the host.

### Changed
- One approximate token counter (`context/tokens.py`), shared by the context
  meter, `/context` and the prompt inspector, deferring to the same langchain
  function and per-model tuning the compactor uses. There were three
  estimators and no two agreed — and the compactor's is the one that decides
  when history is summarised away, so a panel disagreeing with it could have
  shown "7 % used" while turns were being dropped.
- `UsageStore.list` takes a `thread_id` filter. The CLI was fetching 2,000 rows
  by time and filtering in Python, which silently under-reported a session's
  own cost on a busy machine.
- Vertex/Gemini declares its real 1,000,000-token input window, so compaction
  triggers at 700k instead of the 89.6k the 128k fallback produced.
- `sk_search_skill` is always visible instead of sitting behind `tool_search`,
  and the prompt says to search for a saved procedure before deriving how to
  drive an app, device, site or local data source.
- The chat bundle no longer carries a `BudgetPolicy`. It accumulated per-call
  input tokens for the bundle's lifetime against a cap documented as per-call
  and was never reset, so a normal session crossed 120k within a few calls and
  the agent was told to stop calling tools mid-task.
  `YUYUTSAVA_CHAT_BUDGET_TOKENS` is gone; compaction is the control.
- One retry ladder for a 429/503 before the first streamed chunk, not two
  nested ones: the provider's own `max_retries` is pinned low and the quirk
  owns the policy, bounded by `VERTEX_RETRY_BUDGET_SEC` (default 90) as well
  as `VERTEX_MAX_RETRIES`. Retries show on the spinner, and giving up prints
  one line with the session still open.
- The CLI prompt is front-aware: in a terminal, tables belong in the reply as
  Markdown, and there is no Artifacts tab to point at. The desktop-app prompt
  is unchanged.
- The chat banner reports the effective storage backend instead of
  `YUYUTSAVA_SESSIONS_BACKEND`, which only selects the checkpointer default.
- `GRPC_VERBOSITY=ERROR` at both entry points, so gRPC's fork handlers stop
  printing over the renderer on every subprocess.
- `chat_history` rotates at 10 MB.
- Scratch, deliverables and deepagents temp moved from `<ws>/_sandbox`,
  `<ws>/_output`, `<ws>/large_tool_results` and `<ws>/conversation_history`
  into `<ws>/.yuyutsava/{sandbox,outputs,tmp}`. Existing directories are left
  where they are. Workspace skills are read from `<ws>/.yuyutsava/skills/`.
- Docker mounts: `<ws>/.yuyutsava` read-write at `/yuyutsava` (the container
  workdir), the workspace read-only at `/workspace`; `/tmp` is a tmpfs.
  `YUYUTSAVA_DOCKER_EXPORT_DIR` still works but is no longer needed.
- `tr_grep` / `tr_glob` skip `.yuyutsava/` unless it is the search root.
- `main` now carries the full development history and the current code. It had
  been stalled four months behind the working branch.
- Relicensed from MIT to Apache-2.0. Revisions published before 2026-08-31
  remain available under MIT; see [NOTICE](NOTICE).
- `streamlit` moved from a base dependency to the `streamlit` extra — it pulled
  pydeck, altair, tornado and gitpython into every install and is imported
  nowhere in the tree.
- Documentation restructured into `docs/{architecture,reference,guides,design}`
  with an index at `docs/README.md`.
- README rewritten to describe the daemon, desktop app, voice, TODO board,
  memory, MCP and all twelve providers.

### Fixed
- An oversized `task` or `tool_search` result was exempt from offload at every
  size, so it met the 100,000-character result guard with nothing stored
  behind it and the body became unrecoverable. Both now offload on size; the
  `ctx_*` readers stay exempt and are bounded at 40,000 characters per read
  instead, which is what makes that exemption safe.
- The last-resort size guard returned an empty recovery list and advice to
  "write large outputs to a file" for a tool that had already run. It now
  names the artifact readers and how to narrow the call.
- `UsagePolicy` recorded no `thread_id` unless one was pinned at build time,
  which the shared chat bundle cannot do — so chat usage rows could not be
  attributed to a session.
- Ctrl+C during a turn printed a traceback and ended the process: the
  interrupt arrives as `CancelledError` (asyncio cancels the main task), which
  the handlers could not catch. The turn is cancelled and the session stays
  open.
- `vis_*` images written from the standalone CLI landed in SQLite while the
  daemon and the app read Postgres, so the two disagreed about what existed.
- The plain renderer dropped `artifact` events entirely.
- Every subagent's `tr_*` tools were bound to the default sandbox even when its
  `TaskRunnerAgent` had been built with another one — the background tinker's
  sandbox, deliberately placed outside the TODO-board root, was never used.
- The master, its subagents and `spawn.py` each minted their own
  `TaskRunnerAgent` for the same workspace; the default sandbox is now resolved
  before the registry key so they share one.
- `SkillRegistry`, `AgentMemoryStore`, the earcon cache and the webcam/voice
  blob directories hardcoded `~/.yuyutsava` and ignored `YUYUTSAVA_HOME`.
- The daemon never scanned workspace skills at the documented location.
- `.gitignore` matched `diagrams/` at any depth, so `docs/diagrams/` had been
  silently ignored since 2026-06-14 and two intentional assets were never
  committed. The rule is now anchored to `/diagrams/`.
- `scripts/verify_diagrams.py` resolved architecture docs by their old
  repo-root paths and would no longer find them.
- 21 pre-existing broken documentation links, including 17 written as
  `yuyutsava/…` instead of `../yuyutsava/…`.
- `test/test_async.py` hardcoded an absolute home-directory path three times
  and could not run from any other checkout.
- `yuyutsava --help` still advertised "Uses Groq or OpenRouter" long after the
  provider layer grew to twelve providers.

### Removed
- The in-tree DeepFace MCP server (`yuyutsava/mcp_servers/deepface/`) and the
  `deepface` extra. An agent daemon is not the place to host a face-recognition
  service; the `face-watcher` subagent now uses whatever face-recognition MCP
  server you scope to it in `mcp_config.json`.
- The unused `mcp-swagger-ui` dependency (FastAPI serves its own Swagger UI).
- Tracked build and run artefacts: `.DS_Store`, a stray
  `electron-app/.langgraph_api/*.pckl`, a leftover agent deliverable, and a
  stale branch-topology diagram.

---

## [0.1.0] — unreleased

The first tagged release. Development ran from 2026-04-10 and the commit
history is intact; this entry summarises what the project contains at the
point of its first release rather than itemising that history.

Developer machine paths, an unrelated project's files committed by accident,
and LangGraph dev-server state containing real prompts and model output were
removed from the history before publication.

### Added

**Agent core** — a task-runner gateway mediating every filesystem and shell
call through a zone and permission model, with configurable auto-approve
policy and daily caps.

**Two operating modes** — a one-shot/interactive CLI, and an always-on daemon
with an Electron desktop client, sharing one agent stack.

**LLM provider layer** — twelve providers behind one factory: Groq, OpenRouter,
Ollama, OpenAI and any OpenAI-compatible host; Anthropic, Google Gemini, Vertex
AI, AWS Bedrock, Azure OpenAI, Mistral and Cohere via native SDKs. Per-role
overrides let triage run on a cheap local model while the main agent does not.

**Background subagents** — long jobs detach and run independently; completion
wakes the orchestrator on the parent thread instead of blocking a turn.

**Event-driven triage** — filesystem, clipboard, hotkey and app-focus sources
feed a bus, with a cheap triage model deciding what reaches the expensive one.

**Voice** — speech in and out over the same WebSocket as text chat, with wake
word, VAD and barge-in. Configurable STT (faster-whisper, Groq) and TTS (Piper,
ElevenLabs), with a zero-config macOS `say` fallback.

**TODO board** — a persistent planning surface with a dedicated TinkerAgent,
pluggable artifact blocks (including JSX-sandbox and audio), attachments and
card-pinned chat.

**Memory and retrieval** — pgvector-backed semantic memory and skill recall
over a shared retrieval base, with context compaction and tool-result
offloading.

**MCP** — a client manager for stdio and SSE servers, per-agent tool scoping
and `SIGHUP` hot reload.

**Visuals** — charts, styled tables, syntax-highlighted code, math and diagrams
rendered to images the agent can return.

**Storage** — SQLite by default, PostgreSQL for durability and semantic search,
behind a single dialect adapter.

**Cross-platform** — OS-specific primitives confined to `yuyutsava/platform/`,
with Windows and Linux support alongside macOS.

**`/v1` HTTP API** — a frozen contract for external clients, with bearer auth
for non-loopback binds.

[Unreleased]: https://github.com/Abhinav210310453045/YUYUTSAVA-backend/commits/main
