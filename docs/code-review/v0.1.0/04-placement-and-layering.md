# 04 — Placement and layering

Which way dependencies point, and which code lives in the wrong package.
Rules: [01 §3–4](01-standards.md). Numbers: [02](02-metrics.md).

**Headline.** There is no layering yet. Twenty-one of thirty-one sub-packages
form one import cycle. `core/` has become the place code goes when it has no
obvious home, and there is no rule for where a tool, a policy or an endpoint's
logic lives.

---

## R-LAY-01 — One import cycle spans 21 of 31 sub-packages

**Severity** P0 · **Wave** W3 · **Status** Open

**Claim.** Counting module-level imports only (`TYPE_CHECKING` and function-body
imports excluded), 21 sub-packages form a single strongly connected component,
with 9 direct two-way dependencies.

**Evidence.** `scripts/measure_code_health.py --details`:

| Two-way dependency | One direction | Other direction |
|---|---|---|
| `agents` ↔ `core` | `agents/base_sub_agent.py:43` | `core/engine.py:54` |
| `agents` ↔ `cli` | `agents/tinker/agent.py:28` | `cli/agent_stack.py:25` |
| `agents` ↔ `daemon` | `agents/orchestrator/agent.py:24` | `daemon/bootstrap.py:26` |
| `async_subagents` ↔ `daemon` | `async_subagents/watcher.py:36` | `daemon/bootstrap.py:35` |
| `channels` ↔ `daemon` | `channels/plugin.py:24` | `daemon/bootstrap.py:37` |
| `core` ↔ `llm` | `core/model_router.py:41` | `llm/base.py:32` |
| `core` ↔ `skills` | `core/engine.py:55` | `skills/injector.py:18` |
| `core` ↔ `storage` | `core/config.py:46` | `storage/sessions/config.py:13` |
| `core` ↔ `tools` | `core/engine.py:58` | `tools/search.py:28` |

The component: `agents, artifacts, async_subagents, audio_io, channels, cli,
context, conversation, core, daemon, events, llm, mcp, memory, prefs, sessions,
skills, storage, todoboard, tools, visuals`.

**Consequence.** No part of the system can be imported, tested or reasoned about
in isolation. Any change can close a cycle somewhere else (`R-IMP-01`), and the
only local defence is another deferred import (`R-IMP-02`). This is the
condition the 2026-08-08 review called `F-S05`/`F-K03`; `ports/` fixed the
dependency *types*, but the module graph is unchanged.

**Fix.** Adopt the layer map in STD-LAY-1 and remove the two-way dependencies in
the order `R-LAY-02` gives. Record the remaining violations in a shrinking
allowlist.

**Verification.** `test/architecture/test_import_layers.py` green, with an
allowlist that only shrinks. `measure_code_health.py`: `subpackage_two_cycles` 0,
and no cycle involving `core`, `llm` or `storage`.

---

## R-LAY-02 — Lower layers import the composition roots

**Severity** P1 · **Wave** W3 · **Status** Open

**Claim.** Libraries depend on the applications that use them.

**Evidence.**

| Dependency | Where | Remedy |
|---|---|---|
| `core` → `daemon.usage`, `daemon.budget_policy` | `core/engine.py:494,499,1208,1314` | budget and usage policies to `policy/builtin/`; the usage store to `storage/` |
| `storage` → 11 domain packages, incl. `daemon.task_store_unified`, `daemon.usage` | `storage/factory.py:134-294` (14 function-body imports) | stores register with the factory instead of the factory importing them |
| `agents`, `async_subagents`, `channels`, `cli` → `daemon.channels` | `agents/orchestrator/agent.py:24`, `async_subagents/watcher.py:36`, `channels/plugin.py:24`, `cli/async_hitl.py:29` | `AskPrompt` and the router protocol to `ports/channels.py` |
| `channels` → `daemon.web.services.decision_service` | `channels/plugin.py` | a decision port |
| `async_subagents` → `daemon.interrupt_format` | `async_subagents/watcher.py` | move the formatter beside its data |
| `agents` → `cli` | `agents/tinker/agent.py:28` | public `build_retrieval_stores` in `storage/factory.py` |
| `llm` → `core.config` | `llm/base.py:32`, `llm/providers/__init__.py:15` | LLM settings to `llm/settings.py` |

**Consequence.** Using the agent stack without the daemon, or the daemon's
storage without the web layer, means importing both.

**Verification.** As `R-LAY-01`.

---

## R-PLC-01 — `core/` is a grab-bag

**Severity** P1 · **Wave** W3 · **Status** Open

**Claim.** Only a third of `core/`'s modules are about assembling agents.

**Evidence.** Every module, with its responsibility and verdict:

| Module | Responsibility | Belongs in |
|---|---|---|
| `engine.py` | the three master builders, **plus** CLI log setup, `--print-tools`, Mermaid export, sandbox cleanup | `core/` for the builders; the rest to `cli/` (`R-PLC-04`) |
| `agent_profiles.py` | capability matrix | `core/` ✓ |
| `agent_context.py` | reads session and agent path from the run config | `core/` ✓ (only `agents/` uses it) |
| `tool_result.py` | size guard for tool results | `core/` ✓ |
| `tracing.py` | Langfuse integration | `core/` ✓ |
| `streaming.py` | graph driver **plus** CLI prompting, audit writes, daemon iterator, UI-event mapping | split (`R-PLC-03`) |
| `config.py` | 13 LLM settings classes, Docker/local settings, limits, events config, daemon config, search config | split (`R-PLC-02`) |
| `filesystem_prompt_policy.py`, `permission_policy.py`, `retrieval_injection_policy.py`, `subagent_gate_policy.py`, `tool_filter_policy.py`, `voice_style_policy.py` | `Policy` implementations | `policy/builtin/` |
| `docker_sandbox_backend.py` | Docker execution backend, CLI-only | `docker_sandbox/` |
| `model_router.py` | model routing **plus** the price table | routing stays; prices to `llm/pricing.py` |
| `pricing.py` | live price fetch keyed by provider | `llm/pricing.py` |
| `tool_registry.py` | adapter over `discovery/` | `discovery/` |
| `exceptions.py` | Tavily/Exa errors only | beside the search tools |
| `policy.py` | `permissions.json` loader, daemon-only | `daemon/` |
| `permission_middleware.py` | command-safety rules; no middleware left | rename `command_safety.py` |
| `prompts.py` | CLI system prompt sections | `agents/` shared prompts, public names |
| `text_utils.py` | small text helpers | leaf `yuyutsava/text.py` |

**Consequence.** `core` is imported by almost everything and imports almost
everything, which is why it sits in five of the nine two-way dependencies.

**Fix.** The moves above, each committed together with its importer updates. No
compatibility shims: every consumer is in this repository.

**Verification.** `core/` holds only agent assembly; `verify_imports.py`, the
fingerprint and the framework contract stay green after each move.

---

## R-PLC-02 — `core/config.py` mixes unrelated configuration

**Severity** P1 · **Wave** W3 · **Status** Open (legacy `F-S09`)

**Evidence.** 825 lines:
- 13 LLM settings classes and their factory (`:519-558`)
- `EventsConfig` with file I/O (`:638-730`)
- `DaemonConfig` (`:733-800`)
- `SearchConfig`
- Docker and local settings
- limits and timing
- an empty section header left by an earlier move (`:563-564`)

**Consequence.** Every package that needs one setting imports all of them,
together with `core/__init__.py`. That dependency closes the `core` ↔ `storage`
and `core` ↔ `llm` cycles.

**Fix.** Split by owner:
- LLM settings → `llm/settings.py`
- `EventsConfig` → `events/config.py`
- `DaemonConfig` → `daemon/config.py`
- environment helpers → `config/env.py`

`core/config.py` is removed.

---

## R-PLC-03 — `core/streaming.py` holds four unrelated jobs

**Severity** P1 · **Wave** W3 · **Status** Open

**Evidence.**
- CLI prompting on stdin/stderr (`:115-275`).
- Interrupt audit persistence.
- The graph driver and the daemon event iterator (`:528-858`).
- Mapping tool names to UI events (`:330`, `:657-665`).

`daemon/web/routers/sessions.py:24` imports the private
`_artifact_event_from_result`. The file also has 25 `print` calls.

**Fix.**
- The graph driver and `StreamEvent` stay in `core/`.
- Permission prompts and terminal printing move to `cli/render/`.
- Tool-to-event mapping becomes a registry that `visuals/` and `artifacts/` fill.

---

## R-PLC-04 — Command-line tooling inside the agent builder

**Severity** P2 · **Wave** W3 · **Status** Open

**Evidence.** In `core/engine.py`:
- ANSI logging setup (`:106-197`)
- the `--print-tools` JSON (`:276-296`)
- Mermaid PNG export and sandbox cleanup (`:1618-1687`)

Only `cli/` and `daemon/main.py` use any of it.

**Fix.** Move to `cli/`; logging setup to its own module.

---

## R-PLC-05 — No rule for where a tool lives

**Severity** P1 · **Wave** W3 · **Status** Open

**Evidence.** 15 tool factories in 13 files, following at least five conventions:

| Convention | Example |
|---|---|
| a generic package with one module | `tools/search.py` |
| a domain's `tools.py` | `context/tools.py`, `todoboard/tools.py`, `visuals/tools.py`, `memory/tools.py` … |
| a second tools module in a domain | `memory/agent_memory_tools.py` |
| a tool module named after one tool | `discovery/search_tool.py`, `daemon/submit_tool.py` |
| a non-agent under `agents/` | `agents/db_tools/tools.py` |
| a private function in an agent module | `agents/orchestrator/agent.py::_make_ask_user_tool` |

**Consequence.** Finding a tool means searching; adding one means guessing.

**Fix.** STD-PLC-1:
- `<domain>/tools.py` with `make_<domain>_tools()`
- the search tools move to `discovery/`
- `agents/db_tools` → `storage/introspect_tools.py`
- the `tools/` package is removed

---

## R-PLC-06 — Two engines run a conversation turn

**Severity** P1 · **Wave** W3 (placement) + W5 (behaviour) · **Status** W3 done · W5 open

**Evidence.**
- The one-shot CLI turns through `sessions/runner.py:169-242`
  (`run_session` → `astream_agent`).
- The REPL and the daemon turn through `conversation/service.py`
  (`ConversationService`). It repairs interrupted tool calls before a turn and
  retries empty turns; the runner does neither.
- The service imports three private helpers from the runner
  (`conversation/service.py:38-42`).
- The package name `sessions/` also collides with `storage/sessions/`.

**Consequence.** A fix to turn handling lands in one front and not the other.
The one-shot path can end a turn in silence, which the REPL path was fixed not to do.

**Fix.**
- **W3:** done — `CoalescedTicker`, `count_memory_files`, `resolve_session` and
  `ResumeFailed` are public in `conversation/sessions.py`; the one-shot runner and
  its banner are `cli/session_run.py`; `sessions/` is gone. `resolve_session`'s
  stderr warning is now a `print` below `cli/` — carried to W5 with the other
  `print`→logging work.
- **W5:** a separate `fix:` commit makes the one-shot command run through
  `ConversationService`. It changes behaviour, so it is not a refactor.

---

## R-PLC-07 — `io/` and `audio_io/` split one concern

**Severity** P2 · **Wave** W3 · **Status** Open

**Evidence.**
- `io/` holds device and backend adapters: `audio.py`, `stt.py`, `tts.py`,
  `wake.py`, `platform.py`.
- `audio_io/` holds the voice pipeline built on them: `announcer.py`,
  `earcons.py`, `sentence.py`, `synth.py`, `vad.py`, `blobs.py`.

The split is documented nowhere.
- `io` shadows a standard-library module name.
- `io/platform.py` has zero importers and duplicates `platform/hostprofile.py:85-93`.
- `io/audio.play_audio` has zero callers.
- `audio_io/blobs.py` persists clips to disk, which is a storage concern; its
  consumers are `storage/purge.py:147` and the converse router.

**Fix.**
- Merge into `yuyutsava/voice/`: `backends/` (stt, tts, wake, device) plus the
  pipeline modules.
- Delete the dead code.
- `blobs.py` → `storage/voice_blobs.py`.

---

## R-PLC-08 — Docker sandbox split across two places

**Severity** P2 · **Wave** W3 · **Status** Open

**Evidence.**
- The CLI-only backend is `core/docker_sandbox_backend.py`.
- `docker_sandbox/` holds only a Dockerfile and a README. Both are left over
  from a tutorial: the build command points at `tutorials/docker_sandbox/Dockerfile`
  (no such path), and the README tells readers to "run goog".
- `workspace/README.txt` is a tutorial fixture inside the package, referenced
  only by the scenario prompts (`cli/commands/scenarios.py:33,51`).

**Fix.**
- The backend moves to `docker_sandbox/backend.py`, and the build instructions
  are corrected.
- The fixture moves out of the package, or is deleted along with any scenario
  whose prompts are stale.

---

## R-PLC-09 — The web layer keeps schemas and logic in routers

**Severity** P2 · **Wave** W3 (schemas) + W4 (logic) · **Status** Open

**Evidence.**
- `daemon/web/schemas/` exists, yet five routers define request models inline:
  `cli_attach.py` 4, `channels.py` 3, `feedback.py` 2, `visuals.py` 2, `logs.py` 2.
- `daemon/web/routers/converse.py` is 1,099 lines of voice and chat session
  logic with no service behind it (`R-RDB-01`).

**Fix.** Schemas move to `schemas/`; converse logic becomes
`services/converse_session.py`.

---

## R-PLC-10 — Pricing knowledge in three places

**Severity** P2 · **Wave** W3 · **Status** Open

**Evidence.**
- `core/model_router.py:51-160` holds the price table, `load_price_table`,
  `estimate_cost_usd` and `is_priced`.
- `core/pricing.py` fetches live prices by guessing the provider from substrings
  of `base_url` (`:47-53`, `:100-114`).
- The price file name is a constant in one module (`pricing.py:41`) and a
  string literal in the other (`model_router.py:89`).

**Fix.** `llm/pricing.py` owns prices. Live lookup becomes a provider method, so
provider knowledge stays in `llm/providers/`.

---

## R-PLC-11 — Dead code

**Severity** P2 · **Wave** W3 · **Status** Open

Each item below was verified to have zero references across `yuyutsava/`,
`test/` and `scripts/`, or references from tests only where noted.

| Code | Note |
|---|---|
| `agents/orchestrator/spawn.py` | abandoned and never registered; still listed in `storage/events/roles.py:18` and `test/storage/test_store_roles.py:39` (legacy `F-K04`) |
| `io/platform.py` | duplicates `HostProfile` |
| `io/audio.py:80-94` `play_audio` | |
| `agents/task_runner/types.py` | a compatibility re-export with no importer |
| `core/prompts.py:286,309` `ASYNC_SUBAGENT_GUIDANCE`, `async_subagent_guidance` | |
| `ports/retrieval.py` `VectorSearcher` | |
| `models/tool_messages.py:184-193` `ToolNotice`, `is_tool_notice` | |
| `cli/render/plain.py:310` `_fmt_args` | |
| `audio_io/earcons.py` `ensure_earcons`, `generate_default_earcons`; `audio_io/__init__.py` `announcer_from_env` | the docstring says earcons materialise at runtime; nothing calls them |
| `mcp/loader.py:136-151` `result["error"]`, `done` | written, never read |
| `conversation/repair.py:241` `needs_repair` | used by tests only |
| `llm/factory.py:159-167` `supports`, `Capability` | no production caller, despite a docstring claiming every capability has one |
| `core/config.py:563-564` | an empty section header |

**Fix / Verification.** Delete, updating any test that exercises only the dead
code. Then `verify_imports.py` and the package's tests.

---

## R-PLC-12 — Names that shadow other modules

**Severity** P2 · **Wave** W3 (partial) · **Status** Open

**Evidence.**
- Standard-library names: `yuyutsava/io/`, `yuyutsava/platform/`,
  `visuals/math.py`, `visuals/code.py`.
- The third-party SDK's name: `yuyutsava/mcp/`, which imports that SDK as `mcp`.

**Fix.**
- `io/` disappears in `R-PLC-07`.
- `visuals/math.py` → `visuals/latex.py`, `visuals/code.py` → `visuals/code_image.py`.
- **Carried:** `platform` has 16 package-root importers and underpins OS
  invariance; `mcp` is already contained by the `test/mcp_layer/` naming. Each
  rename is its own change, with no other edits in it.
