# 05 — Readability, KISS, and documentation

Code a reader cannot hold in their head, complexity nobody needed, and comments
that say something the code does not do.
Rules: [01 §5–7](01-standards.md). Numbers: [02](02-metrics.md).

**Headline.** Eleven functions exceed 200 lines and one reaches cyclomatic
complexity ≈130. Two builders take more than thirty parameters. A hundred and
seventy-six phrases narrate project history instead of describing behaviour, and
at least fifteen comments describe code that no longer exists.

Oversized *classes* are a responsibility problem and are stated once, as
`R-SOL-01` in [06](06-solid-dry.md).

---

## R-RDB-01 — `converse` is a 771-line endpoint

**Severity** P1 · **Wave** W4 · **Status** **Done** (`W4`). The router accepts the socket, routes `?mode=dictate`, parses the query and delegates: 771 → **105** lines, module 1,099 → 303. The frame types live in `web/services/converse_session.py` and the connection in `converse_connection.py`.

**Claim.** `daemon/web/routers/converse.py:329` `converse` is 771 lines with
approximate cyclomatic complexity 130. At the 2026-08-08 review it was 740 lines
and 116.

**Evidence.** `measure_code_health.py --details`: longest function and most
complex function in the package. The router module is 1,099 lines and has no
service behind it (`R-PLC-09`); `_run_voice_turn` (`:661`) adds 117 more.

**Consequence.** Every voice or chat frame type is a branch inside one function.
A change to one frame's handling requires reading all of them, and none can be
tested alone. It is the least changeable code in the system, and it sits on the
path of the planned computer-use work (`docs/design/computer-use.md`).

**Fix.** Characterization tests for the frame sequences first, then a
`ConverseSession` service in `daemon/web/services/`: one handler per frame type,
with the router reduced to accepting the socket and delegating.

**Verification.** Characterization tests green before and after. `converse` under
60 lines; no function in the new service above complexity 15.

**Progress (W4).** The net came first: 20 characterization tests across
`test/web/test_converse_frames.py` (the text path and the protocol's error
handling), `test_dictation_frames.py`, and a rewritten `test_voice_ws.py`
(`R-TST-11`). Then two extractions into
`daemon/web/services/converse_session.py`:

| | `converse` | router module |
|---|---|---|
| at review (`f4ec45c`) | 771 | 1,099 |
| after the dictation split | 801* | 1,040 |
| after the spoken-turn split | **706** | 947 |

\* it grew before it shrank: the frame-contract work added the `origin`/replay
handling the old count predated.

`DictationSession` took the second dispatch loop; `SpokenReply` took the spoken
turn — sentence chunking, the TTS queue and worker, audio framing, and the
persist-on-clean-finish rule — along with the `C901` entry it carried
(`converse.py` 2 → 1). The four `nonlocal` mic guards became one `SpeakingState`
on the connection, and the five per-connection callables a turn body needs
became one `TurnPlumbing`, which is what let the body move out at all.

A third extraction took the utterance/barge-in machinery into
`UtteranceRouter` — what a captured utterance becomes: a turn, a queued
follow-up, or nothing. `converse` **706 → 594**, the module 947 → 837.

Both of its paths transcribed and then applied the same confidence gate, written
out twice; the gate is now stated once. The copies had already drifted — the
talk-over path strips the transcript before testing it for emptiness and the
turn path does not, so a whitespace-only transcript is dropped by one and runs a
turn on the other. That divergence is preserved here and fixed separately, so
the refactor stays behaviour-preserving.

It also earned the logic its first tests. Barge-in could previously only be
reached through a live socket with barge-in enabled — and it is off by default,
which means the mic gate drops the very frames that would exercise it, so it had
**no coverage at all**. `test/web/test_utterance_router.py` now drives it
directly: 13 tests covering confident talk-over, noise, whitespace, a
low-confidence guess, the disabled case, and a failed transcription (which must
not kill the reply).

A fourth took the inline-ask protocol into `InlineAsks` — building the prompt,
carrying the ownership that keeps a permission prompt out of somebody else's
session, and awaiting the answer from either the hub or the run-local future.
`converse` **594 → 558**.

| extraction | `converse` | module |
|---|---|---|
| at review (`f4ec45c`) | 771 | 1,099 |
| `DictationSession` | 801* | 1,040 |
| `SpokenReply` | 706 | 947 |
| `UtteranceRouter` | 594 | 837 |
| `InlineAsks` | 558 | 802 |
| `ConverseFrames` | 438 | 681 |
| `ConverseConnection` | **105** | 303 |

\* it grew before it shrank: the frame-contract work added the `origin`/replay
handling the old count predated.

`ConverseFrames` is the one the finding actually asked for: **one handler per
frame type**. The seven types dispatch from a table instead of a 174-line
if-chain, and the two judgements the audio frame makes — is the mic gated, and
what does this utterance become — are named methods rather than nested
conditionals. The loop now only parses and dispatches; what a frame *does*
belongs to a collaborator.

`ConverseConnection` took the rest: the handshake, the replay, the frame pump,
starting turns, and closing down without cutting a live reply off. The seam is
the one the code already implied — everything above `send_lock` is endpoint work
(accept, the dictate branch, query parsing, `manager.open`), everything below is
the connection — so `converse` is now 105 lines of exactly that, and the router
module went 1,099 → 303.

The thirteen things the router resolves travel as one `ConverseSpec`, and the
three env knobs as one `VoicePolicy`. The pipeline arrives as a **factory**:
both WS test files patch `converse_router.VoicePipeline`, and resolving that
name inside the new module would have detached the patch silently — the failure
mode being not a red test but two files loading real STT models.

`converse`'s cyclomatic entry fell out of the ratchet entirely (C901 19 → 17
across the branch).

Three stale things in the module docstring surfaced while moving it, all now
fixed: it documented the ask frame as `{"type":"ask","payload":{...}}` when the
code has always sent `"ask"` — which is almost certainly where
`test_converse_ws`'s `ask["payload"]` assertion came from (`R-TST-11`) — it
claimed a speech onset during a turn cancels it, which stopped being true when
the mic gate landed, and it carried a comment for a constant that had already
moved.

**Remaining.** 105 lines against a 60-line target. What is left is genuine
endpoint work — parse the query string, open a conversation, hand over the
socket — so the last 45 would come from moving the query parsing behind a
constructor on `ConverseSpec`, which is worth doing only if it reads better
rather than to hit the number.

---

## R-RDB-02 — Functions longer than a screen, many times over

**Severity** P1 · **Wave** W4 · **Status** Open (legacy `F-K01`)

**Evidence.** 11 functions over 200 lines, 38 over 100, 88 over 60 (of 2,590).

| Function | Lines | Complexity≈ | Shape |
|---|---|---|---|
| `daemon/web/routers/converse.py:329` `converse` | 771 | 130 | `R-RDB-01` |
| `agents/task_runner/tools.py:217` `bind_tools` | 589 | — | 12 tool closures sharing captured state (`:254-777`) |
| `daemon/bootstrap.py:1111` `build_daemon` | 526 | — | down from 927, still over the old target of 200 |
| `cli/commands/chat_repl.py:857` `run_chat_repl` | 504 | 67 | 14 parameters, 5 boolean flags, nesting depth 6 at `:1185`, closures `_on_retry` (`:955`) and `_read_input` (`:1133`) |
| `cli/agent_stack.py:133` `build_agent_stack` | 307 | — | 19 parameters |
| `core/engine.py:896` `build_cli_deepagent` | 276 | — | 33 parameters |
| `context/tools.py:63` `make_context_tools` | 260 | — | 7 tool closures |
| `todoboard/tools.py:123` `make_todo_tools` | 258 | — | tool closures |
| `core/engine.py:1179` `build_orchestrator` | 239 | — | hand-wires two policies (`R-SOL-04`) |
| `daemon/main.py:338` `_async_main` | 218 | 36 | |
| `cli/cli.py:47` `_build_parser` | 204 | — | a flat argument list plus hand-dispatched subcommands (`R-KIS-01`) |

**Consequence.** Each function is a unit of change that no one can review in one
sitting or test in parts. The closures in the tool factories share mutable
captured state, which no reader can see at a glance.

**Progress (W4).** Two of the eleven are done.

`converse` 771 → 105 — see `R-RDB-01`.

`bind_tools` 609 → a package, one module per family. All twelve closures
captured exactly the same three things (`agent`, `workspace_root`,
`agent_name`), so they travel as one `ToolContext` and each family unpacks it
into the names the bodies already used — the tool bodies moved verbatim.

| module | tools | longest factory |
|---|---|---|
| `files` | read, write, delete | 111 |
| `search` | ls, glob, grep | 126 |
| `shell` | execute_in_sandbox, run_python, execute | 137 |
| `web` | fetch_url | 107 |
| `ask` | ask_user | 42 |
| `host` | sysinfo | 30 |

Nothing in the package is over 150 lines, and the split added no ratcheted lint
(C901 17 → 16, from the old file's entry leaving).

The characterization net was written first and earned its place twice: every
argument name taken from the old docstring was wrong (there is no
`task_description` argument anywhere), and the move initially dropped three
module globals — `_registry`, `_default_policy`, `_default_consent` — which the
net caught as a `NameError` rather than a mystery at runtime. One test imported
two private helpers that moved; it was missed on the first sweep because that
grep was piped through `head`.

`build_daemon` 556 → 449, continuing the module's own pattern rather than
inventing one: it already had seven `build_*` functions returning `*Subsystem`
dataclasses, and four more sections had the same shape.

| new | lines | inputs |
|---|---|---|
| `build_mcp` | 16 | workspace |
| `build_models` | 41 | none (all from the environment) |
| `build_channels` | 72 | opts, store, the storage fallbacks to announce |
| `wire_spillover_recovery` | 48 | 6 — a verb, because it installs callbacks and returns nothing |

The web block is deliberately left: it consumes roughly thirty names built
before it, so extracting it would relocate a parameter list and add `PLR0913`
violations rather than reduce anything. A composition root wiring everything
together is the one place a long function is honest.

Two mistakes worth recording, both caught, neither committed. An insertion
anchored on a `class` line rather than its decorator slid between
`@dataclass(frozen=True)` and `AsyncSubagentSubsystem`, leaving the class a
plain class and the decorator on a function — legal Python, so ruff passed; the
daemon test collection caught it. And `build_channels` is `async`, and its call
site was written without `await`: the unbound-names gate does not look for that,
ruff does not either, and no test boots `build_daemon`, so **the live daemon boot
caught it** — exactly the failure mode `test_bootstrap_no_unbound_names.py` was
written about. Both now have a standing check: every `*Subsystem` is asserted to
be a real dataclass, and every call to an async builder is asserted to be
awaited.

**Fix.** Per STD-KIS-2 and §6:
- tool factories become small classes or module-level functions over a bound context;
- the REPL loop becomes a `ReplLoop` class with a command registry (`R-SOL-02`);
- `build_daemon` and `_async_main` become subsystem builders of at most 150 lines;
- `_build_parser` moves to argparse subparsers.

Characterization tests come first wherever a test does not already pin the behaviour.

**Verification.** `functions_over_200_lines` 0, `functions_over_100_lines` ≤ 10,
fingerprint byte-identical.

---

## R-RDB-03 — Parameter lists of 33, 32 and 25

**Severity** P1 · **Wave** W4 · **Status** **Done** for all three named. `build_cli_deepagent` 33 → **7** and `build_tinker_agent` 32 → **6** behind `MasterDeps`; `create_app` 25 → **3** behind `AppDeps`, and `make_app` — a 25-argument pure forwarder — is deleted with `daemon/web/server.py`. Two wide builders remain (`build_agent_stack` 19, `build_tinker_stack` 12); they are W4's exit criterion rather than this finding's subject.

**Evidence.** 14 functions take more than 10 parameters and 38 take more than 7.

| Function | Parameters |
|---|---|
| `core/engine.py:896` `build_cli_deepagent` | 33 — 14 of them typed `Any \| None` |
| `core/engine.py:1420` `build_tinker_agent` | 32 |
| `daemon/web/app.py:73` `create_app` / `daemon/web/server.py:14` `make_app` | 25 each |
| `cli/agent_stack.py:133` `build_agent_stack` | 19 |
| `daemon/conversation_manager.py:81` `ConversationManager.__init__` | 15 |
| `cli/commands/chat.py:31` `run_chat`, `chat_repl.py:857` `run_chat_repl` | 14 each |

`cli/cli.py:390-422` and `cli/commands/chat.py:31-47` pass the same 11 keyword
arguments, recomputed from the same `args`.

**Consequence.** Call sites are unreadable and every new dependency touches every
caller. A misordered or forgotten keyword silently falls back to its default.

**Fix.** Parameter objects typed with `ports` protocols:
- `ChatOptions` for the two CLI fronts
- `MasterDeps` for the master builders
- `AppDeps` for the web app

**Verification.** `functions_over_10_params` ≤ 3.

---

**Fixed (W4).** `create_app` took 25 keyword arguments and `make_app` repeated
all 25 to forward them — three lists (two signatures and the call between them)
that nothing checked against each other. `AppDeps`
(`daemon/web/app_deps.py`) replaces them; `create_app` now takes `hub`, `host`
and `deps`, and both entries leave the `PLR0913` allowlist (35 → 31 across the
branch; functions over ten parameters 13 → 11).

`make_app` is **deleted rather than converted**, and `web/server.py` with it.
Its docstring called it a "backwards-compatible alias" for "historical
imports", but the only importer was this repo's own `bootstrap`, so there was no
caller to keep compatible — and its `WebChannel` / `WebHub` re-exports now come
from `services/stream_service`, where they live. That also retires a docstring
which was four sentences of Phase-2-through-Phase-6 narration (`R-DOC-02`).

`AppDeps` sits beside `web/deps.py` and the two are easy to confuse, so each
docstring now names the other: this is what the app is *given* at startup, that
is how a router *asks* for it per request through `app.state`.

**Verification.** 17 call sites were rewritten from the AST so every argument's
source text carried over verbatim. Live on a restarted daemon: 62 routes
mounted, and the endpoints that read these dependencies all answer —
`/v1/settings/runtime` returns the real voice toggles and disabled subagents,
proving `runtime_settings` and `subagent_roster` reached `app.state` — plus a
five-turn conversation, which is `conversation_manager` on the same path.

## R-RDB-04 — Names that mean two things, and things with two names

**Severity** P2 · **Wave** W3 · **Status** Open

**Evidence.**

- **Four "Policy" concepts:** the `Policy` enum in `core/agent_profiles.py`, the
  `Policy` protocol in `policy/base.py`, `PermissionsPolicy` (a `permissions.json`
  loader) in `core/policy.py`, and `PermissionPolicy` in
  `core/permission_policy.py`. `core/engine.py:46-52` imports the enum as
  `Policy` beside `LangChainPolicyAdapter`, which takes the protocol.
- **A module named after something it no longer holds:**
  `core/permission_middleware.py` contains command-safety rules; the middleware
  class is gone (`:20-22`).
- **Two unrelated artifact stores:** `context/artifacts.py` (offloaded tool
  results) and `yuyutsava/artifacts/` (user-visible artifacts);
  `artifacts/tools.py:31` imports the other one.
- **Same names, different signatures:** `conversation.EventSink`/`AskHandler` and
  `async_subagents/watcher.py:63-64`.
- **A public alias of a private function,** imported by three packages:
  `core/engine.py:604-606` `context_middleware = _context_middleware`.
- **Needless aliases:** `_SS`/`_SF` for `StorageSettings`/`StoreFactory`
  (`cli/agent_stack.py:219-220`, `agents/tinker/agent.py:85-86`).
- **A snake_case flag among kebab-case ones:** `--generate_agent_graph`
  (`cli/cli.py:151`).

**Fix.**
- The enum becomes `PolicyKind`.
- `core/policy.py` becomes the daemon's `permissions_file.py`.
- `permission_middleware.py` becomes `command_safety.py`.
- The context store becomes `offload_store`.
- The duplicate type names are renamed.
- The alias becomes the function's real name.
- `--generate-agent-graph` gets a hidden `--generate_agent_graph` alias.

---

## R-RDB-05 — Magic numbers and repeated literals

**Severity** P2 · **Wave** W4 · **Status** Open

**Evidence.**

- **`60_000`:** repeated at `core/config.py:740-741,769,771,775,777` and
  `core/engine.py:1447`.
- **An async concurrency limit of `8`:** `core/engine.py:709,912,1353,1434`,
  `agents/orchestrator/agent.py:85` and `async_subagents/cap_policy.py:44`.
- **Role strings:** `"cli"` ×7, `"orchestrator"` ×9 and `"tinker"` ×12 in
  `core/engine.py`, although `AgentProfile.role` exists.
- **Dashboard scroll step and notice lifetimes:** `cli/render/dashboard.py:236,239,425,438,515-516,685,695`.
- **A 50 ms "let the write drain" sleep:** `cli/commands/chat_repl.py:553`,
  `cli/commands/prefs.py:66,75`.
- **A cap hard-coded into a tool description sent to the model:**
  `context/tools.py:97` says "capped at 40,000 chars" instead of reading
  `MAX_SLICE_CHARS` (`context/artifacts.py:54`).
- **Display cut-offs:** `context/tools.py` `500`, `120`.

**Fix.** Named constants beside their use; profile fields for role-specific
values. Tool descriptions are built from the constants they describe.

---

## R-RDB-06 — Deep nesting

**Severity** P2 · **Wave** W4 · **Status** Open

**Evidence.**
- depth 8: `visuals/charts.py:36` `render_chart` (at `:59`)
- depth 6: `cli/commands/chat_repl.py:1185`
- depth 5: `core/streaming.py:612-665`, `context/meter.py:429-451`, `llm/quirks/loop_affinity.py:66-74`

**Fix.** Early returns and extracted helpers (`_events_for_message(m)` in
streaming); a dispatch table for chart types.

---

## R-KIS-01 — Subcommands dispatched by hand in front of argparse

**Severity** P2 · **Wave** W4 · **Status** **Partly done.** The commands were defined in two places that could disagree — and did, hiding four from `/help` (`R-BUG-12`). `SUBCOMMANDS` in `cli/cli.py` is now the single definition and generates both the parser epilog and the dispatch. They are still dispatched in front of argparse rather than by subparsers, which is deliberate: subparsers change which command lines are accepted, and that is a product decision, not a refactor.

**Evidence.** `cli/cli.py:292-308` inspects `argv[0]` for `chat`, `daemon`,
`attach` and `prefs` before argparse runs. Because argparse never sees them, the
help text must apologise: "Subcommands (not listed below): chat, daemon, attach,
prefs." (`:54`).

**Fix.** argparse subparsers with lazily imported handlers; `--help` lists them.

---

**Fixed (W4), though not as subparsers.** The commands were named in two places
— a sentence in the parser description (`"Subcommands (not listed below): chat,
daemon, attach, prefs"`) and an if-chain in `main` — with nothing tying them
together. They happened to agree; the slash commands did not, and four working
ones went undocumented for it (`R-BUG-12`).

One `SUBCOMMANDS` registry now drives both: `--help` renders its summaries, and
`dispatch` routes by its names. `main` is 4 lines, `dispatch` is 7.

**Not argparse subparsers, deliberately.** `daemon`, `attach` and `prefs` each
parse their own argv, and the flags in `_build_parser` belong to the task/chat
path. Real subparsers would change which command lines are *accepted* — today
`yuyutsava --workspace X chat` and `yuyutsava chat --workspace X` both work,
because the subcommand is matched before argparse sees anything. That is a
user-visible contract on a daily-use tool, and reshaping it is a separate
decision from removing the duplication.

The registry also states the Ctrl+C rule instead of leaving it implicit: only
the async paths get the quiet exit 130, because `daemon` installs its own
handler and `attach` catches its own — which the live check confirmed (Ctrl-C
detached it cleanly, exit 0).

**Verification.** `test/cli/test_subcommands.py` pins the surface, the routing,
and the invariants that matter: an unknown first word is a *task* rather than an
error (so `yuyutsava "summarise this"` keeps working), a word that merely starts
like a command (`chatter`) is a task too, and importing the CLI does not import
the daemon — its ~195 modules stay behind the handler. Negative-controlled by
deleting a command from the registry. All five paths were then driven for real:
`--help`, `prefs list`, `daemon --status`, `attach` (attach then Ctrl-C), `chat`,
and the bare task path.

## R-KIS-02 — Work done and then undone, and structure without a use

**Severity** P2 · **Wave** W4 · **Status** **Partly done.** The renderer item
closed with `R-SOL-03`: `RichChatRenderer` no longer subclasses `ChatRenderer`,
so there is no smoother to build and discard. The engine item is closed: the
docker and local paths share one build, and `execution_mode` only picks the
backend, the exec backend and the prompt (`_docker_sandbox` builds the
container). Docker mode had no test at all — the fingerprint builds local mode
only — so `test/core/test_execution_modes.py` pins it first: mounts, scratch
root, exec binding, prompt, and that tools, middleware and subagents match
local mode. Both modes' prompt, tools and backend were byte-identical before
and after, and a real turn ran `uname -s; pwd` in each: `Linux` and
`/yuyutsava/sandbox` in the container, `Darwin` and the host sandbox locally.
The TTS item is closed (`8d26cce`, a `fix:`): ElevenLabs writes once, at the
path it was given. `exit_code` is gone (`return 0`). The input is parsed once:
`CommandLine.parse` splits the line into a lower-cased head and case-kept
arguments, and each handler reads that instead of stripping and splitting
again; `_handle_slash` lost its unreachable `/new` branch. `dispatch` had no
behavioural test — only a source-text one — so `test/cli/test_repl_dispatch.py`
came first and passes unchanged on the old code. Only the two policy items are
open; they need a decision (see below).

**Evidence.**

- `cli/render/renderer.py:51-53` — `super().__init__` builds a `TokenSmoother`
  that the subclass immediately replaces with `None`.
- `core/engine.py:1074-1171` — the docker and local branches each build the tool
  registry, the prompt and memory block, `create_deep_agent` and `AgentBundle`;
  only the backend and prompt differ.
- `io/tts.py:111-116` — the ElevenLabs path writes the same bytes to two files.
- `cli/commands/chat_repl.py:947,1360` — `exit_code = 0` is never reassigned.
- `cli/commands/chat_repl.py:1168-1209` — the input is stripped once, then
  stripped and split three more times.
- `core/policy.py:26-28` — two policy names are "recognised" but behave exactly
  like `propose`.
- `policy/types.py:85-98,159-167` — fields that exist only to reproduce
  inconsistencies nobody decided on.

**Fix.** Remove the redundancy. Implement or delete the placeholders. Decide the
policy inconsistencies once, in a `fix:` commit if behaviour changes.

---

## R-KIS-03 — Scrolling re-wraps the whole transcript

**Severity** P2 · **Wave** W4 · **Status** Open (suspected cost; not measured)

**Evidence.** `cli/render/dashboard.py:551-556` recomputes wrapped row counts for
up to 5,000 scrollback lines on every scroll event.

**Fix.** Cache wrapped row counts per width; invalidate on width change or append.
**Verification.** A timing test on a full buffer before and after.

---

## R-DOC-01 — Comments and docstrings that contradict the code

**Severity** P1 · **Wave** W4 · **Status** Open

**Claim.** At least fifteen docstrings or comments describe behaviour the code
does not have.

**Evidence.** (Package-level docstrings are listed separately in `R-INIT-06`.)

| Where | Says | Code |
|---|---|---|
| `agents/__init__.py:7`, `agents/base_sub_agent.py:9,161`, `agents/task_runner/tools.py:4,213` | "the 4 tr_* tools" | `bind_tools` returns 12 (`tools.py:795-799`) |
| `agents/base_sub_agent.py:177-180` | `can_write_skills` is opt-in, "Default (False)" | the default is `True` (`:71`), deliberately |
| `core/agent_profiles.py:16-19` | "descriptive, not yet prescriptive… builders do not consume it" | `_policy_middleware` and `_shared_master_tools` read it (`core/engine.py:627`) |
| `core/engine.py:762-763` | deferred to keep `langgraph_api` optional | the modules import only `yuyutsava.policy.*` |
| `core/engine.py:1004-1005` | budget and usage policies are "daemon-only" | `CLI_PROFILE` and `TINKER_PROFILE` declare both (`agent_profiles.py:137-139,193-195`) |
| `core/engine.py:776-779,940,1174-1176,1192-1195` | refers to `PermissionMiddleware`; adapter collapsing "is a separate step (4.7)" | the middleware is gone; `collapse_policy_adapters` is applied at `:874,1124,1157,1329,1415,1596` |
| `agents/orchestrator/agent.py:47-56` | the async fields "stay duck-typed (object \| None)" | typed with `ports` protocols |
| `cli/async_hitl.py:1-2` | "standalone CLI deepagent, no daemon" | its first class defers to the daemon |
| `llm/providers/__init__.py:3-5` | adding a provider needs "nothing else" | three other modules change (`R-SOL-02`) |
| `context/tools.py:97` | a read is capped at 40,000 chars | the cap is `MAX_SLICE_CHARS` |
| `policy/adapter.py:374-376` | no policy reads the run config for the thread id | `context/offload_policy.py:127` does |
| `models/interrupts.py:5,9` | `engine._prompt_permission()`, `PermissionMiddleware` | renamed and removed |
| `prefs/injector.py:1-3` | the block is orchestrator-only, per "PHASE_2_PLAN" | injected for CLI and tinker too (`core/engine.py:839-842`); the plan is not in the repository |
| `cli/cli.py:7` | "see RESTRUCTURE_HANDOFF.md §5" | no such file |
| `daemon/web/routers/converse.py:42` | the ask frame is `{"type":"ask","payload":{...}}` | both emitters send `{"type":"ask","ask":{...}}` (`:587`, `services/stream_service.py:101`). The Electron store reads `msg.ask \|\| msg.payload`, so clients work; the never-run test asserted the docstring's shape and failed the moment a runner executed it (`R-TST-11`) |
| `cli/render/console.py:18` | mirrors the palette "in chat_repl.py" | it is `cli/render/plain.py:24-29` |
| `context/meter.py:491-494` | explains a lazy import by `core.prompts` | the function no longer imports it |
| `cli/commands/scenarios.py:33,41,51` | prompts use `/yuyutsava/workspace/…` virtual paths and `read_file`/`write_file` | the tool contract is `tr_*` with real paths (suspected stale) |

**Consequence.** A reader who trusts the comment is wrong. For
`can_write_skills` that is a permission question; for "4 tools" it is the audit
of what an agent can do.

**Fix.** Rewrite each to describe current behaviour (STD-DOC-3). Replace counts
with a description or a test (STD-DOC-5).

---

## R-DOC-02 — Project history narrated in the code

**Severity** P2 · **Wave** W4 · **Status** Open

**Claim.** 176 phrases across 108 files narrate how the code got here ("Phase 4
step 4.2", "Step 2.5b", "was:", "no longer", "used to be") instead of what it does.

**Evidence.**
- **Most phrases per file:** `daemon/bootstrap.py` 22,
  `storage/pg/migrations.py` 6, `storage/__init__.py` 5,
  `daemon/task_registry.py` 4, `daemon/web/services/decision_service.py` 4.
- **About twenty module docstrings open with a migration step:**
  `core/filesystem_prompt_policy.py:3`, `memory/store_unified.py:3`,
  `context/*_unified.py:3`, `policy/{adapter,base,types,ask}.py:3`,
  `visuals/store_unified.py:1-9`.
- **A 22-line implementation plan for an out-of-scope feature:**
  `agents/face_watcher/agent.py:13-34`.
- **Comments such as "until now there was no way to tell":** throughout
  `cli/commands/chat_repl.py`.

**Consequence.** The history is already in the commit log, where it is
accurate. In a docstring it goes stale the day the next change lands, and it
buries the one sentence a reader needs.

**Fix.** Rewrite to current behaviour; move plans to issues or `docs/design/`.
Decision references cite ADR paths (STD-DOC-4).

**Verification.** `history_narration_phrases` 0. The heuristic also matches
legitimate phrases such as "no longer than", so each remaining hit is reviewed.
