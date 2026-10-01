# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).
While the version stays `0.x`, minor bumps may contain breaking changes.

## [Unreleased]

### Added
- **The chat REPL opens on a proper screen, and says who said what.** A wordmark
  centred in the pane it is drawn in — the split view redirects the banner into
  the left pane, so measuring the whole terminal put the art off to the right —
  with a fit ladder down to a three-row mark and then the bare name as the
  terminal narrows. Under it, `user@host · date · time` and every session fact
  as one labelled row: the label bold in its own colour, the value in a second
  colour the transcript never uses, so a session id cannot be read as a tool
  result. The langgraph-api notice is a `warning` row among them rather than the
  first thing on screen, and its check runs on a thread beside the session
  lookup instead of holding up the first frame. The one-shot path draws the same
  banner (`cli/render/startup.py`).
- **Your own messages are in the transcript.** Nothing printed them: the REPL
  dispatched the line straight into the turn, and in the split view
  prompt_toolkit erases it on submit — so the transcript read as an
  unattributed monologue, and the line was missing from the replay printed back
  to the scrollback on exit. `user_message()` joins the renderer protocol; the
  REPL calls it only where the terminal has not already echoed, so nothing is
  printed twice.
- **One colour system in place of three.** `yuyutsava/ansi.py` gains a role
  layer — one hue per kind of thing, with state carried by weight and glyph —
  and the Rich theme and the dashboard's prompt_toolkit styles are derived from
  it rather than restating it. Two ramps, because the CLI cannot set the
  terminal's background; `YUYUTSAVA_THEME` and `COLORFGBG` choose, and a
  terminal without `COLORTERM` gets 256-colour escapes rather than the
  parameters of a 24-bit sequence printed as text.
- **Each answer is drawn inside a dotted frame** in the assistant's colour,
  built as a top edge, per-line rails and a bottom edge rather than a
  `rich.panel.Panel` — a Panel must know its whole contents before it can close,
  and prose is committed block by block so a long answer appears while it is
  being written (`cli/render/prose_frame.py`).

### Changed
- **The context column reads in words.** Its rows were the meter's own keys —
  `in`, `out`, `cached`, `calls`, `compacted` — which are a wire format the app
  also reads and say nothing about which direction is which. On screen they are
  now `Sent`, `Received`, `Reused`, `Model calls` and `Compactions`, the window
  line is `35.5k of 1.0M used`, and each row's name and number are styled apart
  instead of forming one block of grey. The column lists facts about the
  session exactly as the banner does, so it wears the banner's key and value
  colours: one screen, one way of saying "this name, that number". Its status
  row carries the same working mark as the transcript, and a notice too long
  for the column continues under itself.
- **A warning is two lines on the transcript's ladder.** Its label and origin
  on one row, the message on the next at the body indent, so the message wraps
  under itself instead of under the label — where its continuation read as a
  second warning. Every label in the transcript is punctuated the same way now:
  `❯ you:`, `◇ yuyutsava:`, `✳ skills:`, `⚠ UserWarning:`.
- **A wrapped line continues under itself,** and breaks at a space. Rich wraps its own output to the
  pane, so the lines that reach the wrapper are the ones nothing formatted — a
  `print`, a log record, a warning — and their second row began at column zero,
  reading as a new statement rather than the rest of the one above. Rows after
  the first are now indented, unless the pane is too narrow to spare the cells,
  where the text matters more than the alignment.
- **The mark that says a turn is alive moves.** A turn waits — on the first
  token, on a tool, on a subagent — and a status line that does not move cannot
  be told apart from a wedged process, which is the moment a reader reaches for
  Ctrl+C. Both surfaces now draw an animated braille cell that walks the
  palette as it turns, and settles the moment tokens start landing, because
  arriving prose says the turn is alive better than a spinner does
  (`cli/render/pulse.py`; the phase maths is shared so the scrolling
  transcript's `Live` region and the split view's status bar cannot drift).
- **Each speaker's marker is punctuated and separated from what they said.**
  `❯ you:` and `◇ yuyutsava:`, with your message now in a dotted frame of its
  own in your colour rather than the assistant's, so which side of the
  conversation a block belongs to survives a glance. The plain renderer
  punctuates identically and stays unframed: a box in piped output is noise.
- **Prose streams instead of arriving a paragraph at a time.** The transcript
  commits a markdown block only once that block is complete, so between blank
  lines nothing was on screen at all. Text that has arrived but is not yet a
  complete block is now shown as provisional rows inside the open frame,
  replaced as more lands and dropped when the finished block prints, so nothing
  can appear twice. Prose is also paced through the token smoother the plain
  renderer already used; the rule for when to pace is stated once, so
  `YUYUTSAVA_REPL_SMOOTH` and `YUYUTSAVA_REPL_SMOOTH_CPS` govern both
  renderers. Piped output is not paced and is unchanged.
- **Skills retrieval reports through the renderer.** The names a turn matched
  were an INFO log record, which reaches the transcript pane without passing
  through a renderer and therefore landed unindented at column zero between the
  user's message and the first tool call. Retrieval now publishes them to
  listeners (`skills/injector.add_recall_listener`, the shape the retry
  listener uses) and the renderers draw one row, fitted to the pane with a
  count of whatever did not fit.
- **A Python warning is one themed row.** The stdlib's two-line format — an
  absolute `site-packages` path, the category, the message, then an echo of the
  offending source line — went into the pane verbatim. It is now the module
  name, the category and the message on one indented row. Nothing is filtered:
  a real warning still arrives.
- **langgraph-api 0.9.0 → 0.15.1, out of end of life.** The in-process server
  background subagents run on was six minors behind, where support ends at
  two. It moves with its family — langgraph-runtime-inmem 0.35.1, langgraph-cli
  0.4.32, langgraph 1.2.12, langgraph-sdk 0.4.5 — and with Starlette 0.52 → 1.7,
  which langgraph-api 0.15 requires. deepagents, langchain and langchain-core
  are unchanged. langgraph-api and langgraph-sdk are imported directly and now
  declared directly. A new framework contract checks every keyword the host
  passes `run_server` (which takes `**kwargs`, so a renamed one would be
  ignored, not rejected) and every keyword the watchers pass the SDK.

### Fixed
- **The langgraph-api notice no longer says `pip install -U`.** That moves one
  package off `uv.lock` and cannot reach a supported minor on its own. It now
  says to upgrade through the lock; CONTRIBUTING.md has the commands.
- **Nothing could be copied out of the split view.** A full-screen application
  that reports mouse events receives every click and drag itself, so the
  terminal never saw a drag to turn into a selection — the transcript was
  readable and unquotable, and the modifier that bypasses mouse reporting
  differs by terminal (Option in iTerm2, Shift in most others), so there was no
  answer that worked everywhere. Ctrl+O now hands the mouse back, and takes it
  again; the status bar says which mode is on, because a mode nobody can see is
  a mode nobody can leave. The wheel is the price while selection is on, and
  PgUp/PgDn/End are unaffected.
- **Scrolling the transcript stuttered.** Working out how far it was allowed to
  scroll re-wrapped the entire 5,000-line scrollback — regex tokenisation plus
  three uncached `unicodedata` lookups per character — on every wheel tick, and
  a wheel sends several per flick. Row widths and per-line row counts are now
  cached, and the total is memoised against a version counter the buffer bumps
  when its content changes. Measured over 5,000 distinct lines at 88 columns:
  85 ms per tick before, 1 µs when nothing has changed, 0.23 ms to recount
  after new output. The application's own repaint interval also drops from
  0.5 s to 0.12 s; every write already invalidates, so it is a safety net, but
  at half a second it was the longest anything could sit unseen.

### Fixed
- **On Vertex, every chat turn failed, even "hi", because of one MCP tool's
  schema.** Version 2.9.0 of the Google Drive MCP server declares
  `manage_accounts.account_id` as `"type": ["string", "null"]`, which is valid
  JSON Schema. The Vertex SDK's converter upper-cases the whole list into
  `"['STRING', 'NULL']"`, and protobuf rejected it with a `ParseError` on every
  request that bound the tool. The Vertex provider now rewrites type lists into
  the equivalent `anyOf` (plus `nullable`) before binding. It also checks each
  tool with the SDK's own converter and leaves out, with one warning, any tool
  Vertex still cannot express, so one bad schema costs that tool rather than the
  conversation (`llm/quirks/vertex_tool_schema.py`). The Gemini API provider
  converts such schemas correctly and is unchanged.
- **A permission prompt in the split terminal view could not be answered at
  all.** The card rendered, `approve/reject>` appeared in the pane, and typing
  did nothing: the prompt was a blocking `input()` while the full-screen
  prompt_toolkit application held stdin in raw mode, so every keystroke went to
  the application and the read never completed. The turn sat waiting on an
  answer that could never arrive. Reading a line is now a seam
  (`cli/line_reader.py`) that the front owning the terminal installs once —
  the same shape as the retry listener and the meter bus — and all four CLI
  prompts go through it, including the background-subagent bridge and
  `yuyutsava attach`, which had the same latent bug. Ctrl+C, Ctrl+D and
  shutdown refuse an open question rather than hanging it, because a question
  that cannot be answered is never consent. An AST tripwire (with a negative
  control) fails the build if a new prompt calls `input()` directly.
- **The context meter's total did not add up, and could fall while the
  conversation grew.** One chat read `26.9k / 1.0M` on its first call and
  `23.5k / 1.0M` seven calls later, with the provider reporting 13.4k and then
  27.6k for those same two prompts — and the panel printed `in 27.6k` four rows
  under `23.5k`. Two causes. The total was a pure sum of character estimates
  that never consulted `usage_metadata`, and one global correction factor
  multiplied *every* row on *every* publish, so when the factor moved the whole
  conversation changed size. The estimate was also ~2x high for a structural
  reason (≈20k characters of JSON tool schema reach Gemini as
  `FunctionDeclaration` protos) and the old `[0.5, 2.0]` clamp pinned the real
  0.499 at its floor, hiding the error. Now the headline is **anchored on the
  provider's reported input tokens** and only growth since is estimated — right
  after a call it is `input + output`, both measured, and it can only fall when
  content actually leaves the window. The static prefix and the messages get
  **separate** correction factors, fitted from a conversation's first call
  (which carries almost no message tokens and so measures the prefix exactly)
  and thereafter by subtraction; factors are remembered per model, so a second
  chat starts calibrated. Rows are reconciled to sum to the total exactly, and
  both fronts now drop the `≈` from a measured total.
- **The graceful interrupt left the assistant's turn hanging open.** `Ctrl+S`
  cancelled the run and appended the user's message straight after a tool
  result, so history read `ai(tool_calls) → tool(success) → human` with the
  model's turn never closed — and in Gemini's content model a
  `functionResponse` is itself a user-role part, making that two consecutive
  user turns. Two replies came back empty at 60,813 and 60,978 input tokens,
  logged `repaired=0` because neither previously-known shape was present. The
  repair now diagnoses four things rather than matching one string: the
  fabricated cancellation, a tool call with **no result at all** (which the
  function was named for but could not see), an empty assistant message (no
  text, no calls — removed via `RemoveMessage`, since it is self-perpetuating),
  and an unfinished turn, closed with a short assistant message that also tells
  the model where it left off. A resume does not close the turn it is resuming.
- **An empty turn was not retried unless something was repairable.** The
  recovery fired only when the marker check had found something, so a turn that
  came back empty for any other reason got no retry at all — the log read
  `(repaired=0) — surfaced to the user` twice in a row, and the next message
  the user sent went through untouched. The retry is now unconditional and
  still capped at exactly one.
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
