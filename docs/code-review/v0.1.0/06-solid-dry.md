# 06 — SOLID and DRY

Responsibilities, extension points, substitutability, dependencies, and
knowledge written down more than once.
Rules: [01 §9–11](01-standards.md). Numbers: [02](02-metrics.md).

**Headline.** A handful of classes and modules each do the work of four or five.
Adding a provider, a slash command, a stream event or a visual kind means editing
chains, not adding an entry, and one of those chains has already drifted into a
user-visible bug. Small helpers exist in up to eighteen disagreeing copies.

---

## R-SOL-01 — Classes and modules with many reasons to change

**Severity** P1 · **Wave** W4 · **Status** Open

**Claim.** Each unit below holds several responsibilities that share little
state and change for different reasons.

**Evidence.**

| Unit | Size | Responsibilities (method groups) |
|---|---|---|
| `todoboard/exchange.py:92` `TodoExchange` | 623 lines, 30 methods | cards (`add_card`, `get_card`, `update_card`, `delete_card`, `query_board`, `board_snapshot`, `list_card_ids`) · objectives (`add_`/`update_`/`delete_objective`) · notes (`add_note`, `update_note`, `search_notes`, `delete_note`, `assign_note`) · attachments (`attach`, `delete_attachment`, `list_all_attachments`) · generation (`generate_artifact`, `_update_generated`) · events (`_emit`, `list_events`) · indexing (`_schedule_note_index`) · validation (`_guard`, `_check_*`) |
| `async_subagents/watcher.py:350` `AsyncTaskHealthWatcher` | 681 lines, 18 methods | thread discovery · run ingestion · polling · progress emission · transcript fetch · interrupt handling (`_handle_interrupt`, 99 lines) · resume · ask building · orphaned-tool-call healing · error fetch · terminal handling · completion notification · timeouts |
| `cli/render/dashboard.py:244` `ChatDashboard` | 617 lines, 43 methods | application and key bindings · input and asks · turn cancellation · scrolling · notices · **process-wide capture of stdout, stderr, logging and warnings** · meter subscription · width sync · replay |
| `cli/render/plain.py:48` `ChatRenderer` | 530 lines, 29 methods | event rendering · a payload ring buffer (`:253-298`) · environment parsing in the constructor (`:63-77,131-142`) |
| `context/meter.py` (module) | 1,077 lines | `MeterBus` (`:263`) · counters (`:323-352`) · sizing caches (`:356-478`) · prompt segmentation (`:485-562`) · calibration (`:643-708,853-907`) · the policy and pricing (`:712-1058`); 9 mutable module-level stores |
| `cli/commands/chat_repl.py` (module) | 1,360 lines | the REPL loop · banner, help and version notice · permission card · usage, skills and context printers (`:65-258,350-398,589-722`) · usage queries against storage (`_usage_rows`) · history rotation · runtime settings (`_LazyRuntimeSettings`) |

`core/streaming.py` and `core/config.py` are the same problem, recorded as
placement findings `R-PLC-03` and `R-PLC-02`.

**Consequence.** Any change to one responsibility re-tests all of them, and each
file is edited by unrelated work. That makes it the likeliest merge conflict and
the hardest review.

**Fix.**

- **`TodoExchange`** stays the public facade, delegating to card, objective,
  note and attachment services that share one validator. Routers and tools do
  not change.
- **`AsyncTaskHealthWatcher`** becomes a coordinator over `TaskPoller`,
  `InterruptHandler` and `TimeoutEnforcer`.
- **`ChatDashboard`** splits out `StdioCapture`, `InputBroker` and `ScrollView`.
- **`ChatRenderer`** composes a `PayloadRing` and receives `ReplSettings`.
- **`context/meter.py`** splits into `bus.py`, `sizing.py`, `calibration.py` and
  `policy.py`.
- **REPL printers** move to `cli/render/`; usage queries move to a service.

**Verification.** Each class at most 400 lines. The existing tests for each unit
pass unchanged, and new unit tests cover each extracted part.

---

## R-SOL-02 — Extension means editing chains (open/closed)

**Severity** P1 · **Wave** W4 · **Status** Open (includes legacy `F-S06`, `F-D05`)

**Claim.** Six extension points are `if`/`elif` chains or scattered tables, not
registries.

**Evidence.**

| To add… | You must edit |
|---|---|
| **an LLM provider** | a module in `llm/providers/`; a branch in `core/config.py:519-558` and its error string; a key in the context-budget table `context/config.py:27-35`; the `base_url` substring sniffing in `core/pricing.py:47-53`. The docstring at `llm/providers/__init__.py:3-5` says one line is enough. |
| **a slash command** | one of five dispatch mechanisms in `cli/commands/chat_repl.py:1172-1229` (`_handle_ask_command`, `_handle_settings_command`, inline `split()[0] == "/usage"` / `"/context"` / `"/skills"`, `_handle_slash`, a `"new"` sentinel), plus the `/help` text, which has already drifted from `_SLASH_COMMANDS` (`:231-258` vs `:309-327`: `/help` omits `/asks /approve /reject /reply`) |
| **a stream event kind** | the `if`/`elif` chain in each renderer — they have already diverged (`R-BUG-03`) |
| **a tool whose result becomes a UI event** | hard-coded tool names in `core/streaming.py:330,657-665` |
| **a visual kind** | the `KINDS` tuple, the `_load` chain in `visuals/render.py:14-40`, and a new `@tool` in `visuals/tools.py` |
| **a policy** | the seven-way chain in `core/engine.py:472-520`, where any `phase` other than `"pre"` means post |

The remote-attach event formatter is another chain eight levels deep
(`cli/remote_attach.py:160-195`).

**Consequence.** Each addition is a scavenger hunt through several files. A
missed site fails silently: a provider falls back to the default context budget
(`R-BUG-01`), a renderer drops an event (`R-BUG-03`), a help screen lies.

**Fix.** One registry per extension point, with every consumer derived from it:

- Providers declare aliases, settings type, context budget and price lookup.
- Commands declare name, help and handler; `/help` is generated.
- Event kinds map to handlers per renderer, with a test that every kind is handled.
- Tools register their event mapping.
- Visual kinds register renderer and tool together.
- Policies map to factories.

**Verification.** A registry test per extension point. Adding a fake provider,
command or event kind touches exactly one file.

---

**Progress (W4).** Two steps, in order.

First one list: `SLASH_COMMANDS` replaced the completer's hand-kept dict and the
hand-written body of `_print_help`, which had already drifted (`R-BUG-12`).

Then one dispatcher. `ReplCommands` (90 lines) replaced five calling conventions
inside the loop — `_handle_ask_command` and `_handle_settings_command` returning
`bool`, `_handle_slash` returning sentinel objects, and `/usage`, `/context` and
`/skills` compared inline — with a single `dispatch()` returning
`NOT_A_COMMAND | HANDLED | QUIT`. The loop's 72-line command section is now six
lines, and `run_chat_repl` is **476 → 431**.

The one piece of shared mutable state is the session: `/new` replaces it, and
the loop reads it back from the dispatcher rather than keeping its own copy.
That is the part most likely to break silently — a stale copy would leave the
REPL running turns against the old thread while the banner showed the new one —
so it was checked live: `/new` printed a new session id, and the next turn
answered `NONE` to a fact stated before the rotation.

The bodies of the four handlers are untouched. What changed is that the loop no
longer knows any of their conventions, so a new command cannot be added without
the dispatcher being asked about it.

## R-SOL-03 — Substitution and interface width (Liskov, interface segregation)

**Severity** P1 · **Wave** W4 · **Status** Open (legacy `F-S12` closed for stores; recurs here)

**Evidence.**

- **No renderer contract.** `RichChatRenderer` subclasses `ChatRenderer` only to
  reuse its ring buffer. It overrides eight display methods and inherits about
  twelve it never uses (`_render_tool_call_compact`, `_render_todos`,
  `_zone_chip`, …).
  - The REPL reaches into a private attribute: `cli/commands/chat_repl.py:1339`
    `renderer._smoother.aclose()`.
  - The dashboard duck-types its renderer: `cli/render/dashboard.py:644`
    `attach_renderer(renderer: Any)`, then reads `status_text`, `tool_in_flight`
    and `on_change`.
- **A settings protocol two implementations do not satisfy.**
  - `LlmSettings` requires `api_key` and `base_url` (`core/config.py:88-96`).
  - `VertexSettings` and `BedrockSettings` have neither (`:318-420`), yet
    `llm_settings_from_env` is declared to return `LlmSettings`.
  - Callers work around it with `getattr` (`core/pricing.py:105-107`).
- **An interface wider than its implementations.** Every provider's `build` must
  accept `disable_reasoning`; only the OpenAI-compatible provider uses it
  (`llm/base.py:74-80`, `llm/factory.py:148-152`).

**Fix.**
- A `ChatRendererProtocol` (render, begin and end turn, pause, retry notice,
  status, `aclose`, ring commands) with composition instead of inheritance.
- `LlmSettings` narrowed to what every provider has (`model`).
- `disable_reasoning` passed as a capability-gated option.

**Verification.** A protocol-conformance test for each renderer; type checks on
`llm_settings_from_env` callers.

---

**Progress (W4).** `build_cli_deepagent` took 33 parameters and
`build_tinker_agent` 32, **27 of them the same 27**, repeated at every call
site. They now take a `MasterDeps` (`core/deps.py`) and 7 / 6 parameters
respectively; `PLR0913` 35 → 33.

What stays out is what genuinely differs: where the CLI agent runs and who reads
its output (`execution_mode`, `docker_settings`, `local_settings`, `front`), and
which card the tinker agent is bound to.

**The two lists had already drifted**, which is the argument for the change
rather than a side note. `budget_tokens` defaulted to `None` for the CLI and
`60_000` for tinker, and `tinker_stack` never passes it — so that default was
live. It is deliberate: a conversation's input grows with its own history, so a
cumulative budget that never resets would eventually refuse every turn, while
the tinker agent works a single card and a cap keeps a runaway think loop from
spending the session. Collapsing the two lists naively would have silently
picked one. `MasterDeps.budget_tokens` means "no budget", and
`build_tinker_agent` applies the named `TINKER_BUDGET_TOKENS` when it is unset,
so the divergence is now stated instead of hidden in a default.

**Verification.** The agent fingerprint is byte-identical across all nine
configurations — which is exactly what it exists for, and what makes a change of
this shape checkable at all rather than argued. The harness splits its overrides
by field name so every configuration stays expressible. Live on a restarted
daemon: a five-turn conversation with a permission ask, and a CLI chat turn.

## R-SOL-04 — Dependencies declared as `Any` while the protocols exist (dependency inversion)

**Severity** P1 · **Wave** W4 · **Status** Open (legacy `F-S05` partly closed; `F-D01` partly closed)

**Claim.** The `ports/` protocols fixed `OrchestratorDeps`, but the other
builders and bundles still erase their dependency types. The orchestrator also
bypasses the capability profile for two of its policies.

**Evidence.**

- `core/engine.py:896-930` `build_cli_deepagent`: 14 parameters typed
  `Any | None`, including stores for which `ports` already defines `ArtifactStore`,
  `MemoryStore`, `SummaryStore`, `TranscriptStore`, `UsageStore`, `RuntimeToggles`
  and `CapEnforcer`.
- `core/engine.py:229-236` `AgentBundle`: fields typed `Any` with the comment
  "duck-typed to avoid cycle".
- `cli/agent_stack.py:133-154`, `agents/tinker/subagent.py:173-183` (every factory
  parameter `object | None`), `conversation/service.py:381` (`drive: Any`).
- `agents/face_watcher/agent.py:60-89` and `agents/file_organizer/agent.py:24-53`
  re-declare the base class's parameters typed more weakly
  (`cap_enforcer: object | None` against `CapEnforcer | None` at
  `base_sub_agent.py:76`).
- `conversation/service.py:91,160,163,498`: the I/O-agnostic engine defaults its
  identity to `"cli"`.
- `core/engine.py:1308-1390`: `build_orchestrator` wires `BudgetPolicy` and
  `UsagePolicy` by hand instead of through `_policy_middleware(ORCHESTRATOR_PROFILE, "post")`.
  The profile is therefore not the single source of truth it was built to be.

**Consequence.**
- Type checkers and readers cannot tell what a builder needs.
- A wrong store is accepted silently.
- A profile change for the orchestrator's budget has no effect.

**Fix.** `MasterDeps` typed with `ports`. Protocols for the remaining
`AgentBundle` fields. Required identity arguments. The orchestrator routes
budget and usage through the profile.

**Verification.**
- Zero `Any`/`object` dependency parameters in `core/engine.py` and `cli/agent_stack.py`.
- A profile test flipping the orchestrator's budget changes the assembled stack.
- The fingerprint is otherwise byte-identical.

---

## R-DRY-01 — Environment variables parsed about eighteen ways

**Severity** P1 · **Wave** W2 · **Status** Done (`21db0c8`, `2eff394`, `b239429`, `fc7dbde`, `dbd8e30`). The sites the helpers cannot express, and the raising `WAKE_THRESHOLD`, are listed under W2 in [11](11-remediation-plan.md).

**Claim.** Boolean, integer and float environment variables are parsed by
hand-written snippets that disagree on what counts as true.

**Evidence.**

| Accepted as true | Where |
|---|---|
| `1`, `true`, `yes` | `core/engine.py:158`, `core/config.py:756`, `memory/config.py:49,78`, `agents/tinker/agent.py:40`, `cli/agent_stack.py:43`, `daemon/bootstrap.py:851` |
| `1`, `true`, `yes`, `on` | `context/config.py:103`, `context/prompt_inspector.py:44`, `prefs/runtime.py:71` |
| `on`, `true`, `yes`, `1`, `enable`, `enabled` | `io/stt.py:157-159` |
| anything except `0`, `false`, `no`, `off`, `""` | `core/tracing.py:65` and the CLI renderers |

- **The same variable, parsed in several places:** `YUYUTSAVA_ASYNC_SUBAGENTS`
  three times (`agents/tinker/agent.py:40`, `cli/agent_stack.py:43`,
  `daemon/bootstrap.py:851`); `YUYUTSAVA_DEBUG_PLUMBING` three times
  (`core/engine.py:158`, `cli/commands/chat_repl.py:876`, `daemon/main.py:95`).
- **Integer and float parsing with fallbacks:** `core/config.py:750-781` (five
  copies in one method), `memory/config.py:64-94`, `context/config.py:78-90`,
  `core/tracing.py:164-167`, `llm/quirks/first_chunk_retry.py`, `audio_io/vad.py:32-43`.
- **An unguarded float:** `io/wake.py:181`. A bad `WAKE_THRESHOLD` raises and can
  crash-loop the voice subprocess.
- **Only helper available:** `core/config.py:69` `_env`, which is private and
  returns strings.

**Consequence.** `YUYUTSAVA_X=on` works for some flags and silently does nothing
for others.

**Fix.** `yuyutsava/config/env.py`: `env`, `env_flag`, `env_int`, `env_float`.
Every call site migrates **keeping its current accepted values**, passed
explicitly, so the migration changes no behaviour. Unifying the value sets is a
later, separate `fix:` commit if wanted.

**Verification.** Unit tests for the helpers. A grep for
`os.environ.get(...).lower() in` returns nothing outside `config/env.py`.

---

## R-DRY-02 — Message content flattened four ways

**Severity** P1 · **Wave** W4 · **Status** Open

**Evidence.**

| Copy | Joins blocks with |
|---|---|
| `core/streaming.py:55` `flatten_content` | `""` |
| `context/transcript_index.py:60-72` | `""`, a byte-for-byte copy of the above |
| `policy/adapter.py:450-458` | `" "`, falls back to `str()` |
| `async_subagents/watcher.py:134` | `"\n"` |
| `conversation/repair.py:138` | a lazy wrapper that swallows exceptions |

**Consequence.** The same model message produces different text depending on
which subsystem reads it. A fix for a new content-block type (the Gemini
mixed-content bug was one) must be made up to four times.

**Fix.** One `yuyutsava/text.py:flatten_content(content, sep="")`. Each call
site passes the separator it uses today.

---

## R-DRY-03 — The current thread id, looked up three ways

**Severity** P2 · **Wave** W4 · **Status** Open

**Evidence.** `policy/adapter.py:371`, `context/compaction.py:453` and
`context/artifacts.py:83-91`, with `artifacts/tools.py:31` wrapping the last one.
The fallbacks differ: `""` in two, `"unknown"` in `context/artifacts.py`, so
every offload made outside a run shares the key `"unknown"`.

**Fix.** One helper with an explicit `default=` argument; call sites keep their
fallback until a deliberate decision.

---

## R-DRY-04 — Terminal rendering helpers copied between renderers

**Severity** P1 · **Wave** W4 · **Status** Open

**Evidence.**

- **Four static helpers in `cli/render/plain.py:438-577`** duplicate
  `tool_format.compact_summary`, `pretty_body`, `looks_like_error` and
  `error_message`. `tool_format.py:208` says they were "moved"; the originals
  remain, and `tool_format.looks_like_error` has no caller.
- **Nine inline `[:N] + "…"` truncations** in `plain.py` (`:277,317,325,355,441,445,459,466,469`),
  although `tool_format.truncate` exists.
- **A path-extraction loop copied in both renderers,** with a third zone
  classifier:
  - `plain.py:471-503` and `renderer.py:429-447` classify with plain `startswith`,
    without separator handling or `sandbox_root`;
  - `agents/task_runner/zones.py:47-93` is the real zone classifier.

  The zone chip on screen can therefore disagree with the actual permission
  decision.
- **Seven colour palettes:** `plain.py:24-29`,
  `commands/sessions.py:21-26,52-58`, `async_hitl.py:234-299`,
  `remote_attach.py:138-194`, `commands/attach.py:87,107,120`,
  `console.py:19-29`, `dashboard.py:95-106`. Only `sessions.py` checks whether
  output is a terminal.
- **Two duration humanisers** with different formats:
  `commands/sessions.py:40-49` and `async_hitl.py:185-190`.
- **Two versions of the same status decision tree, already disagreeing:**
  `renderer.py:162-217` `status_text` vs `_status_renderable` — only the latter
  shows "Thinking…" when idle.
- **Identical `Live(...)` construction twice:** `renderer.py:104-110,134-140`.
- **Provider-busy classification by exception class-name string, three times:**
  `chat_repl.py:1277-1278`, `renderer.py:87`, `dashboard.py:693`.
- **The permission card re-formats fields** that `panels.py:56-87` already
  formats (`chat_repl.py:354-398`).

**Fix.**
- `tool_format` is the only copy of the formatting helpers.
- `cli/render/palette.py` with terminal gating; one `humanize_duration`.
- The zone chip calls `zones.classify_zone`.
- Status is computed once and rendered two ways.
- `classify_provider_busy(exc)` lives in `llm/`.

---

## R-DRY-05 — Front-end and builder wiring copied

**Severity** P1 · **Wave** W4 · **Status** Open (legacy `F-D06` partly closed)

**Evidence.**

- **HITL ask prompt copied almost line for line:** `cli/async_hitl.py:218-257`
  and `cli/remote_attach.py:126-157`. The background-task event lines are copied
  too (`async_hitl.py:287-304` vs `remote_attach.py:176-194`).
- **Async watcher and bridge wiring:** `cli/commands/chat.py:70-81` and
  `chat_repl.py:1056-1068`.
  - Teardown differs: the one-shot path swallows the watcher error silently and
    leaves `cleanup_local_sandbox` unguarded (`chat.py:126-134`); the REPL guards
    both (`chat_repl.py:1353-1357`).
- **The `_build_host` closure** is copied in `cli/agent_stack.py:346-381` and
  `agents/tinker/agent.py:138-175`, and has drifted: only tinker passes
  `meter_window=False`.
- **In `conversation/service.py`,** the get-state, diagnose and repair sequence
  is written twice (`:351-368`, `:412-427`).
- **In `core/engine.py`:**
  - the orchestrator repeats `_sync_subagent_specs` inline (`:1323-1341` vs `:851-893`);
  - the docker and local branches repeat the build (`R-KIS-02`).
- **Identical `search_tools` overrides:** `agents/general_purpose/agent.py:100-118`
  and `agents/tinker/subagent.py:158-170`.
- **An identical `__init__`:** `agents/face_watcher/agent.py:60-89` and
  `agents/file_organizer/agent.py:24-53`.
- **The start-up environment block:** `cli/cli.py:25` and `daemon/main.py:35`.

**Fix.**
- `prompt_for_ask` and one event-line formatter.
- `start_cli_hitl(bundle)` and `teardown_front(bundle, watcher)`.
- `build_async_host(...)`, deciding `meter_window` once and recording why.
- `_diagnose_and_repair(close_turn)`.
- `_sync_subagent_specs` taking the model and extra middleware.
- A base-class flag for exposing every search tool.
- An `EventSubAgent` base class.

---

## R-DRY-06 — Voice and miscellaneous knowledge repeated

**Severity** P2 · **Wave** W3–W4 · **Status** Open

**Evidence.**

- **The 16 kHz sample rate, three times:** `io/audio.py:19`,
  `audio_io/earcons.py:32-33` (whose comment points at the first),
  `audio_io/vad.py:28`.
- **A mono 16-bit WAV writer, three times:** `io/audio.py:73-77`,
  `audio_io/blobs.py:48-52`, `audio_io/earcons.py:80-84`. A WAV reader appears
  twice: `io/audio.py:104-107`, `audio_io/synth.py:44-48`.
- **Two blocking subprocess wrappers:** `io/tts.py:53-66,149-157`, although
  `platform.process.run_capture` exists.
- **A private copy of `ports.CapEnforcer`:** `tools/search.py:34-39`.
- **Two detectors for offloaded digests,** both depending on the key order of the
  JSON built at `context/digests.py:68`: `context/meter.py:565-566` and
  `context/prompt_inspector.py:58-59`.
- **A hand-written tool-to-argument table that is already wrong:**
  `cli/render/tool_format.py:50,56`.
  - `tr_chmod` is not a tool.
  - `tr_run_python` is keyed on `code`/`script`, but its parameter is
    `script_path` (`agents/task_runner/tools.py:408`).

**Fix.**
- One constant, one WAV module and `run_capture` in the voice package (`R-PLC-07`).
- Import `ports.CapEnforcer`.
- One digest detector in `digests.py`.
- Derive the argument table from the tool schemas.
