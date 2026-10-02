# Model Configuration — one file, every provider, any model, picked in chat

**Status:** Accepted 2026-10-02 — decision recorded in [ADR-005](../architecture/decisions/ADR-005-models-config.md); execution tracked in §6 and §7.
**Scope:** how YUYUTSAVA is told which LLM providers it may call, how it logs in to each, which model each task uses, and how a conversation switches model.
**Related code:**
- [yuyutsava/llm/](../../yuyutsava/llm/) — the provider layer this replaces the configuration of
- [yuyutsava/core/engine.py](../../yuyutsava/core/engine.py) — agent assembly, where the model switch is wired
- [yuyutsava/daemon/bootstrap.py](../../yuyutsava/daemon/bootstrap.py) — the composition root that builds every model

---

## Context

The provider layer (`yuyutsava/llm/`) can technically build 11 provider kinds, but in practice only
Vertex Gemini is used.

What is wrong today:

- **Configuration is env-only.** There is one provider and one model per process, read once at
  startup. `max_input_tokens` is set per provider, not per model.
- **Nothing to discover or switch.** There is no catalog, no discovery and no verification, and no way
  to switch models while running. The CLI doesn't even read the `~/.yuyutsava/.env` that Electron
  writes.
- **Vertex.** Only Gemini works. Claude and the open models on Vertex (Model Garden) can't be reached.
- **Bedrock.** The only option is the default credential chain: no app inference profile, no
  availability check.
- **Logins.** Service accounts, profiles and bearer tokens can't be expressed per provider.
- **No end-to-end testing.** Only fake-SDK and constructor tests exist, and Bedrock is never built.

**The intended outcome, which the owner set as the bar:**

- **One file.** `~/.yuyutsava/models.toml` is the single place every provider, login, model and task
  assignment lives. It is read live: an edit takes effect without a restart.
- **Three ways to fill it, one store.** By hand, through a Setup Wizard (CLI and Electron), or by asking
  the agent. All three go through one store.
- **Switching from any chat surface.** CLI `/model`, the Electron composer chip and voice can all switch
  provider and model mid-chat.
- **Easy for everyone.** Every step is easy for a non-technical person and just as easy for a technical
  one.

The work follows Code Review v0.1.0 (`docs/code-review/v0.1.0/01-standards.md`) and mirrors the wave
format of `11-remediation-plan.md`. It closes the open findings **R-SOL-03** and **R-PLC-10**.

## Decisions taken with the owner (2026-10-02)

| # | Decision |
|---|---|
| D1 | **TOML**, at `~/.yuyutsava/models.toml`. It is written by the CLI wizard, the Electron UI and agent tools, all through **one Python store** that keeps the user's comments (`tomlkit`). |
| D2 | **Secrets are never in models.toml**, only references: `secret:<name>` (stored in `~/.yuyutsava/secrets.toml`, 0600), `env:NAME` or `file:path`. An **opt-in OS user-presence gate** (Touch ID or password, Windows Hello, polkit or password) protects editing and revealing secrets, on all three OSes. |
| D3 | **First run = Setup Wizard.** The CLI shows it right after the wordmark. Electron shows a centred wizard window. It imports what it detects; after setup, `LLM_*` env vars are ignored, with a warning that names the file. |
| D4 | **Pickers everywhere:** CLI `/model`, the Electron composer, per-task defaults, the wizard and agent tools. A task that isn't assigned follows the default. One model can be applied to any set of tasks. |
| D5 | **Live discovery and verification** use free endpoints. "Send a test message" is billable, and it runs only when the user clicks it. |
| D6 | **Scope is global now.** ADR-005 designs the global/workspace distinction and RBAC for later. The code has one layer and no speculative generality (STD-KIS-3). |
| D7 | **Subagents keep their own task setting.** After a chat switch, a **nudge** offers to apply the new model to *all* subagents, or to *specific* ones by name. |

---

## 1. The file — what a user writes

```toml
# ~/.yuyutsava/models.toml          (JSON Schema: `yuyutsava models schema`)
version = 1

[defaults]
model = "gemini-flash"                 # every task without its own pick uses this

[tasks]                                # optional — omit a task to follow the default
triage                 = "groq-llama"
compaction             = "gemini-flash"
"subagent.researcher"  = "sonnet"      # one named subagent

[providers.google-work]                # WHERE + HOW TO LOG IN
type     = "vertex"
project  = "project-4a4a8e80-…"
location = "asia-south1"
auth     = { method = "service_account", key_file = "~/keys/yuyutsava-sa.json" }   # or { method = "adc" }

[providers.openrouter]
type = "openrouter"
auth = { method = "api_key", key = "secret:openrouter" }

[providers.groq]
type = "groq"
auth = { method = "api_key", key = "env:GROQ_API_KEY" }

[providers.aws]
type   = "bedrock"
region = "us-east-1"
auth   = { method = "aws_profile", profile = "work" }
#        { method = "aws_keys", access_key_id = "secret:aws-id", secret_access_key = "secret:aws-secret" }
#        { method = "bedrock_api_key", key = "secret:bedrock" }

[providers.local]
type     = "ollama"
base_url = "http://localhost:11434"    # auth defaults to { method = "none" }

[models.gemini-flash]                  # WHAT TO CALL — alias → provider + id (+ overrides)
provider = "google-work"
id       = "gemini-3.5-flash"

[models.sonnet]
provider = "google-work"
id       = "claude-sonnet-4-5@20250929"
location = "global"                    # per-model: Claude isn't served in asia-south1

[models.groq-llama]
provider = "groq"
id       = "llama-3.3-70b-versatile"

[security]
require_user_presence = false          # true → Touch ID / Hello / password before secret edits
```

- **Model reference:** either an **alias** (`sonnet`) or **`<provider>/<model-id>`**, split on the
  first `/`, so `openrouter/anthropic/claude-sonnet-4.5` works. A full reference needs no
  `[models.*]` entry. Aliases exist for short names and overrides (`location`/`region`,
  `context_window`, `max_output`, `thinking`, `price`, `family`).
- **Family is inferred, with an override.** On Vertex, `claude-*` maps to `anthropic`,
  `<publisher>/<name>-maas` to `maas`, and anything else to `gemini`.
- **Auth methods per type** (validated; each error lists the allowed ones):

  | type | auth methods |
  |---|---|
  | vertex | `adc`, `service_account`, `access_token` |
  | google (AI Studio) | `api_key` |
  | bedrock | `aws_default_chain`, `aws_profile`, `aws_keys` (+session token), `bedrock_api_key` |
  | anthropic · openai · openrouter · groq · mistral · cohere | `api_key` |
  | azure | `api_key` (Entra ID added later as one more method) |
  | openai_compatible | `api_key`, `bearer_token`, `none` |
  | ollama | `none` (default), `bearer_token` |

- **Errors are written for humans.** Each error gives the file and line, what's wrong, what was
  expected and the exact fix. For example:
  `providers.aws.auth.method = "api_key"` — Bedrock logs in with aws_profile, aws_keys,
  bedrock_api_key or aws_default_chain; for a Bedrock API key use
  `{ method = "bedrock_api_key", key = "secret:bedrock" }`.
- **Facts are not config.** Discovered models and availability live in
  `~/.yuyutsava/cache/models/<provider>.json`. models.toml holds only what the user chose.

## 2. Architecture

```
L0 ports/models_config.py   ModelResolver + ConfigWriter Protocols (no first-party imports)
L0 platform/user_presence.py  per-OS presence check (pattern: platform/elevation.py)
L1 llm/config/      schema.py (pydantic, discriminated unions) · loader.py (tomllib → friendly errors)
                    store.py (tomlkit writes, FileLock, atomic replace, compare-generation)
                    secrets.py (ref resolution, secrets.toml 0600) · snapshot.py (immutable ConfigSnapshot)
                    watch.py (watchdog thread → validate → publish snapshot; last-good on error)
                    schema_export.py (JSON Schema) · detect.py (what's already on this machine)
L1 llm/kinds/       one module per provider type: Connection schema, auth methods, families,
                    build(), discover(), verify(), live_price()  (replaces settings.py + providers/)
                    vertex/{gemini,anthropic,maas,discover}.py — bedrock.py — openai_compat.py (groq,
                    openrouter, ollama, openai, openai_compatible) — anthropic, google, azure, mistral, cohere
L1 llm/catalog.py   ModelCard: window, max output, tools, vision, reasoning, price
                    precedence: models.toml override > discovery cache > langchain ModelProfile > kind default
L1 llm/tasks.py     closed TASKS registry + resolve(task, thread override) → ModelRef
L1 llm/quirks/      + history_portable.py (cross-provider history), keep the existing four
L3 policy/model_switch.py   framework middleware: per-call request.override(model=…) from the pinned turn choice
L3 policy/cache_control.py  cache_control kwarg for Claude on Vertex/Bedrock (prompt caching parity)
L4 core/            ModelPool built in the composition root (injected, no module global — DEP-1)
L5 cli/commands/models.py + cli/setup_wizard/ + /model in chat_repl · daemon/web/{routers,schemas,services}/models
   electron-app: SetupWizard, Settings→Models, composer ModelChip
```

**Key runtime rules.** Each rule below comes from the pressure-test and has evidence in the code.

1. **Nothing reads the disk on the event loop.** A watchdog thread loads, validates and resolves
   secrets, then publishes an immutable `ConfigSnapshot` with a generation counter. Resolvers only
   read the snapshot. An invalid hand edit keeps the last good snapshot and surfaces the error in the
   CLI status line, the app banner and `models doctor`. Blockbuster blocks `os.stat` and `os.replace`
   on the loop, which is why the thread is required (`daemon/bootstrap.py:1127`). The `watchdog`
   package is already a dependency (`events/sources/fs.py`).
2. **The selection is pinned per turn.** It is resolved once when the human message arrives and
   stored for that turn. A `/model` switch or a hot reload applies from the **next** turn. Switching
   mid tool loop breaks Anthropic's thinking-block rule.
3. **The switch works per call; the graph is never rebuilt.** `policy/model_switch.py` sits at
   index 0 of yuyutsava's middleware (`core/engine.py:843 _master_middleware`). It replaces
   `request.model` (`ModelRequest.override`, `langchain/agents/middleware/types.py:202`). Tools are
   re-bound to it on every call (`langchain/agents/factory.py:1162-1289`). It is also added to
   **each subagent graph**, keyed by (thread, subagent name) (D7).
4. **Window-sensitive hooks ask per thread.** Today compaction (`context/compaction.py:169,190`), the
   context meter (`context/meter/policy.py:76-84`) and usage (`policy/builtin/usage.py:59`) fix the
   model and window when the agent is built. They change to query the injected resolver by
   `thread_id_from_runtime`. Usage rows also record the **provider type**, so pricing stops guessing
   from the model name.
5. **Pool key = (resolved entry + connection hash, build options, event loop).** Triage
   (`temperature=0, disable_reasoning`) and chat never share an instance. An edit rebuilds only what
   changed. `LOOP_AFFINE` rules stay as they are (`aio/LoopLocal`). Build-time placeholders keep the
   current "own instance per host factory" rule (`bootstrap.py:1113-1118`).
6. **Credentials are passed as objects, never through `os.environ`.**
   - Google: a `Credentials` object (SA or ADC).
   - Bedrock: **we build the boto3 clients** and pass `client=`/`bedrock_client=`. Given `api_key=`,
     `ChatBedrockConverse` still builds its session from the machine's default AWS profile. On the
     owner's machine that profile uses `login_session`, which needs `botocore[crt]`, so a Bedrock
     API key failed because of unrelated `~/.aws` contents (measured 2026-10-02). Our session:
     - never reads `~/.aws`;
     - has an empty credential resolver, so no environment, container or metadata-endpoint lookup;
     - takes a Bedrock API key through botocore's public `register_component("token_provider", …)`.

     Verified at the `before-send` hook: the API key sends `Bearer`, access keys sign with SigV4,
     and `os.environ` is untouched (`test/framework_contract/test_bedrock_contract.py`). The
     `os.environ` write at `langchain_aws/utils.py:365` is on the Nova Sonic path only, and we never
     call it.
   - Vertex MaaS: our own `ChatOpenAI` on `…/endpoints/openapi`. Its `api_key` is a **sync** token
     function (`credentials.refresh` only when the token is invalid). `ChatOpenAI` calls it on every
     request, and for async calls it runs it in an executor thread, so the blocking refresh never
     runs on the event loop. This was verified with a mock transport; an async function would run on
     the loop (`test/framework_contract/test_vertex_model_garden_contract.py`). The langchain MaaS classes hard-code their model
     list, break with `location="global"` and bake in a token that expires.
7. **History portability. Verified locally; see §2a.** An assistant message goes to the
   **exact model that wrote it** unchanged, so signatures and thinking survive. To any other model
   it goes as **portable history: text plus `tool_calls`, nothing else**. `quirks/history_portable.py`
   applies this rule; it changes only what is sent, and checkpoints stay untouched. The writer is
   identified by our own `response_metadata["yuyutsava_model"]` stamp, set by the build wrapper on
   the **first** stream chunk only. Untagged legacy messages are treated as foreign.
8. **Prompt caching parity.** deepagents' caching only fires for `isinstance(model, ChatAnthropic)`
   (`langchain_anthropic/middleware/prompt_caching.py:101`). Claude on Vertex or Bedrock gets
   `Capability.CACHE_CONTROL_KWARG` plus `policy/cache_control.py`. Without it, moving Claude would
   silently cost more.
9. **The app boots unconfigured.** With no usable config, the daemon starts with only the settings and
   `/models` routes. Chat returns `409 setup_required` and Electron opens the wizard. The CLI runs the
   wizard before `_chat_options` (`cli/cli.py:413`) and before `dashboard.start()`.
10. **There is one writer.** The CLI, the daemon API (and through it Electron) and agent tools all use
    `llm/config/store.py`. A write holds `platform.FileLock`, checks the generation it read, and
    rejects a stale write with "changed since you opened it — reload".
11. **Secrets never pass through a checkpoint.** Agent tools ask for a secret **by name**. The CLI
    (`getpass`) or the app (an authenticated daemon endpoint) writes the value straight to
    `secrets.toml`. The agent's resume value is only `{"stored": "secret:<name>"}`. Interrupt
    payloads are persisted (`storage/events/ask_wire.py:39`, `storage/interrupts.py:34`), so the
    value must never go through them.
12. **Our compaction owns summarisation. deepagents' summarizer is excluded** (owner, 2026-10-02;
    revisitable).
    - **The evidence.** deepagents wraps every call in its own `SummarizationMiddleware`
      (`deepagents/middleware/summarization.py:212,908`). Its trigger (0.85 of the window) and its
      `ContextOverflowError` fallback both come from the **build-time** model.
    - **What goes wrong after a switch.** It can summarise too early, once the conversation moves to
      a bigger-window model. Its fallback summary also calls the original model, which may be a
      provider the user has removed.
    - **Ours already runs first.** `YuyutsavaCompactionMiddleware` fires at 0.7 of the window, so in
      practice the deepagents copy only supplied the overflow fallback.
    - **Nothing else catches overflow.** No yuyutsava code handles `ContextOverflowError`, and only
      the genai, Anthropic and OpenAI SDKs raise it.

    So, in M4:
    - **Exclusion.** The summarizer is excluded through a harness-profile `excluded_middleware` entry
      registered at the composition root for every provider type we build. A contract test fails if a
      type is missing.
    - **Overflow fallback.** `YuyutsavaCompactionMiddleware` gains the fallback: on overflow it
      compacts with the `compaction` task model and retries once.
    - **Overflow detection.** Overflow is recognised from `ContextOverflowError` *and* from the
      Bedrock, Mistral and Claude-on-Vertex "too long" errors, through `classify_provider_busy`'s
      sibling in `llm/`.
    - **What is lost.** deepagents' `/conversation_history` offload file (the DB transcript and
      `ctx_history*` already cover evicted turns) and its `write_file`/`edit_file` argument clipping.

13. **Harness prompt tuning follows the active model.** deepagents picks a model-specific
    `system_prompt_suffix` once, from the build-time model. Built-in profiles exist for
    `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5` and the GPT Codex models
    (`deepagents/profiles/harness/`). After a switch, `policy/model_switch` removes the build model's
    suffix and applies the target model's, pinned by a contract test in M4.

14. **Vertex Gemini is built with `ChatGoogleGenerativeAI(vertexai=True)`.** `ChatVertexAI` is
    deprecated in langchain-google-vertexai 3.2.0 and removed in 4.0, and we don't build on an
    end-of-life class (owner, 2026-10-02). These were verified locally against the installed SDKs,
    with no requests sent:
    - genai reaches Vertex with `project`, `location` and `credentials`
      (`…/v1beta1/projects/p/locations/…/publishers/google/models/…:streamGenerateContent`).
    - It retries a 429 or 503 that arrives when a stream opens, and stops at `max_retries`. This was
      mock-transport tested on both backends; `ChatVertexAI`'s gRPC path misses that case.
    - It converts MCP type-list schemas, though it narrows `["string","null"]` to `STRING` and drops
      nullability.
    - It already applies the Gemini 3 dummy thought signature.

    **Every Gemini fix carries over** and is re-validated against genai with a parity test. Each is
    kept as a defence even where the SDK now covers the case, because the same failures can come
    back through it:
    - `gemini_parts` (zero-part messages);
    - `loop_affinity`;
    - `gemini_budget` (thinking on top of the answer);
    - `first_chunk_retry` (owns the retry policy, the wall-clock budget and the CLI's "provider busy,
      retrying" notices; the SDK's own retries are capped so the ladders don't nest);
    - the `vertex_tool_schema` rewrite, generalised to `gemini_tool_schema` so nullability survives;
    - `classify_provider_busy`, extended to genai's `ClientError`/`ServerError`.

    Claude on Vertex keeps `ChatAnthropicVertex`, which is not deprecated.

### 2a. Cross-provider history: verified, not assumed (2026-10-02)

**Method.** Each producer's own SDK response parser built a realistic assistant tool-call turn and a
final answer, with thinking, signatures, redacted thinking and reasoning included. Each target's own
SDK request builder then turned that history into the exact payload it would send. The run is fully
local: proxies point at a dead port and no request was sent.

Five producers were tested:

- Vertex Gemini;
- AI Studio Gemini;
- Anthropic;
- OpenAI-compatible (also covers Groq, OpenRouter, Ollama, Azure and Vertex MaaS);
- Bedrock Converse.

They were sent to nine targets:

- OpenAI-compatible;
- Anthropic, with and without thinking;
- Claude on Vertex;
- Vertex Gemini;
- AI Studio Gemini;
- Bedrock;
- Mistral;
- Cohere.

The harness is [test/framework_contract/test_cross_provider_history_contract.py](../../test/framework_contract/test_cross_provider_history_contract.py).

| History sent as… | Result across 45 producer×target pairs |
|---|---|
| raw (as checkpointed) | **Fails.** See the "Failures with raw history" list below. |
| LangChain standard `v1` blocks (`content_blocks`) | **Not universal.** Anthropic and Bedrock histories still raise on the Vertex and Bedrock builders; thinking text still leaks into other targets. |
| **portable (text + `tool_calls`)** | **Clean on all 45.** No raise and no foreign field in any payload. |

**Failures with raw history:**

- Anthropic → Vertex Gemini raises: "Only text, image_url, and media types are supported!"
- Anthropic → AI Studio raises `Incorrect padding`, because Anthropic's signature is decoded as a
  Gemini one.
- Anthropic → Bedrock raises on `redacted_thinking`.
- Bedrock → Vertex Gemini raises.
- Cohere raises `tool_plan` errors for 4 of 5 producers.
- Anthropic → OpenAI-compatible sends a `redacted_thinking` part.
- Gemini → Anthropic sends a `thinking` block **without a signature**. The Anthropic API requires one,
  so this would be rejected.
- Gemini → Mistral sends `thought_signature` inside a text part.

**Other facts found:**

- `ChatVertexAI` 3.2.4 does **not** patch the Gemini 3 "function call needs a thought signature"
  rule. langchain-google-genai does: `DUMMY_THOUGHT_SIGNATURE` on the first function call of the
  active loop (`langchain_google_genai/chat_models.py:864-904`). The owner runs Vertex
  `gemini-3.5-flash`, so on `ChatVertexAI` a portable or legacy function call in the active loop
  would get a 400. This is one of the reasons the Vertex Gemini family moves to genai (rule 14).
- Producer tags are unreliable. Vertex, Bedrock and OpenAI's parse path set no `model_provider`, and
  `"openai"` can't distinguish Groq, OpenRouter, OpenAI or Vertex MaaS. Hence our own stamp.
- Custom `response_metadata` strings **concatenate across stream chunks** (measured:
  `'gemini_vertexgemini_vertex'`). Only `model_provider` is special-cased, and LangChain's block
  translators read it, so we can't reuse it. Stamp the first chunk only.

**Why stripping loses nothing that is needed.** Turn pinning (rule 2) means the active tool loop is
always written by the model that continues it, so it stays raw. Gemini 3 needs signatures only in the
active loop. Anthropic needs a thinking block only on the last assistant turn of an in-progress tool
loop. Thinking from earlier turns is optional for both.

**Still to confirm live (M9, with owner approval).** The server must accept the portable payloads
(Groq, OpenRouter, Anthropic, Vertex Claude, Vertex Gemini 3). The Vertex dummy signature must also be
accepted. Tool-call ID formats crossing providers (Gemini UUIDs, `toolu_`, `call_`, Bedrock
`tooluse_`) built cleanly everywhere, and Mistral rewrites its own IDs.

**Tasks registry (closed, one source — DRY-3).**

| Task | Label shown to users |
|---|---|
| `chat` | CLI, app chat and voice |
| `orchestrator` | Background tasks and Tinker |
| `triage` | Event triage |
| `subagent` | Subagents, plus `subagent.<name>` |
| `compaction` | Summaries |
| `tier_light` / `tier_standard` / `tier_heavy` | Smart routing; only shown when routing is on |

Today's free-form role strings map one-to-one onto these. The doc-only `file_organizer` is dropped.

## 3. User experience

**CLI first run.** The wordmark appears, then "Welcome — let's connect a model". The wizard uses the
role palette and glyphs from `cli/render`.

1. **Detect.** The wizard shows what it found on this machine and offers to import it:
   - `.env`;
   - gcloud ADC;
   - `~/.aws` profiles;
   - a running Ollama;
   - `*_API_KEY` env vars.
2. **Pick a provider.** Each provider shows a plain-language card: what it is, what you need and a
   link.
3. **Choose a login method.** Only the valid methods appear, with steps:
   - **Vertex:** "Run `gcloud auth application-default login`", or "Choose your service-account
     JSON".
   - **Bedrock:** choose a profile, paste keys or a Bedrock API key.
4. **Verify.** The verify step is free. It names the problem in plain words, for example "This project
   hasn't enabled Vertex AI — open link".
5. **Discover.** Discovered models get availability badges:
   - ✓ available;
   - 🔒 needs access request (Bedrock agreement/entitlement, Model Garden enable);
   - ⌖ not in this region (suggests `global` or a region that serves it);
   - ? unknown.
6. **Pick models.**
7. **Add another provider?**
8. **Set the default** and apply it to tasks. The multi-select lists every task, with "same as
   default" ticked.
9. **Write the file.** The wizard writes `models.toml` and `secrets.toml` and shows the path.

**Electron first run.** A centred Setup Wizard window runs the same steps in the same order through
`/models`, with an **Add another provider** button. After setup, **Settings → Models** offers:

- providers (add, test, rediscover, remove);
- a model list (enable, alias, overrides);
- a task matrix with a "same as default" toggle per row and a "use this for…" multi-apply;
- a security toggle for the presence gate.

**Switching in chat.**
- The CLI has `/model`, a picker grouped by provider. Each row shows the window, price and ✓ tools,
  with the active model marked. `/model sonnet` switches directly.
- The Electron composer has a ModelChip that does the same.
- **Scope:** "this conversation" by default, stored on the session. "Make default" is an option.
- **Subagent nudge (D7):** after a switch, the user sees "Subagents still use gemini-flash. Change
  them?" and can choose:
  - all subagents;
  - specific subagents (multi-select by name);
  - not now.
- The status line, the context panel and the app aside show the **active** model and its real window.

**Agent tools.** These are the `mdl_*` family on the chat/CLI deepagent only, because the orchestrator
stays a plain router:

- `mdl_list`
- `mdl_use`
- `mdl_assign`
- `mdl_add_provider`, which takes secrets out-of-band, by name
- `mdl_discover`
- `mdl_verify`

Changes go through consent (`yuyutsava/consent/`). Secret edits also pass the presence gate when it is
on.

**CLI subcommands.** `yuyutsava models`:

- `setup`
- `list`
- `add`
- `remove`
- `discover`
- `verify [--test-message]`
- `use <ref> [--task …]`
- `assign`
- `doctor`
- `edit`
- `path`
- `schema`
- `secrets set|rm`

## 4. Verification

- **Hermetic.** Each wave runs the full suite before it claims green. The gates are:
  - `scripts/measure_code_health.py`;
  - `scripts/verify_imports.py`;
  - `uvx ruff@0.16.5 check` and `format --check`;
  - `check_lint_ratchet.py`;
  - `test_agent_fingerprint.py`;
  - `verify_framework_contract.py`;
  - `verify_loop_affinity.py`;
  - the `yuyutsava --print-tools` sha256.

  PG tests use `yuyutsava_test`.
- **Live**, marked `billable` and gated by `YUYUTSAVA_ALLOW_BILLABLE=1`. These are **only run after
  the owner approves each batch**, with a cost estimate first. One tool-calling turn is run per row:

  | Provider | Families / auth methods |
  |---|---|
  | Vertex | Gemini (asia-south1, SA + ADC), Claude (global), one MaaS open model |
  | Anthropic | API key |
  | OpenRouter | API key |
  | Groq | API key |
  | Ollama | local, free |
  | Bedrock / Azure | once the owner has accounts |

  The run also includes a **switch-mid-conversation run**: Gemini → Claude → Groq → Gemini with tool
  calls in the history, plus a subagent-nudge run.
- **Discovery and verify** call only free endpoints, but they are still external calls, so they are
  run with the owner present.
- **UX check.** A fresh `YUYUTSAVA_HOME` (empty) is used to walk both wizards end to end, then edit
  models.toml by hand while a chat is open and confirm the next turn uses the new model.

## 5. Risks carried into the work

- **SDK drift.** Every SDK fact the design relies on is pinned by an M0 contract test.
- **Cross-provider history.** This was resolved by local verification (§2a): only portable history
  is accepted by every builder. What remains is server-side acceptance of the portable payloads and
  of the Vertex dummy signature, which is one live turn per target in M9.
- **Presence gate on Linux headless and Windows without Hello.** Each falls back to a password, and
  the spike documents it. The gate is a guard on YUYUTSAVA's own surfaces, not encryption at rest.
- **Lock change.** The owner's `uv tool` install of `yuyutsava` needs `scripts/install_cli.py`
  re-run after the dependency waves.
## 6. Status

| Wave | State |
|---|---|
| M0 | In progress |
| M1 | Not started |
| M2 | Not started |
| M3 | Not started |
| M4 | Not started |
| M5 | Not started |
| M6 | Not started |
| M7 | Not started |
| M8 | Not started |
| M9 | Not started |

## 7. Waves

Work happens on branch `feature/model-config`, cut from the `code-improvement` tip. It stays local,
and commits use the repo-local IITJ identity; it is pushed only when asked. This document and ADR-005 are written in M0.

**Ground rules.** These are carried from 11-remediation-plan.md:

- one concern per commit;
- gates before changes;
- no compatibility shims;
- no billable calls in verification without the owner's go-ahead;
- the stop rule: an unexpected fingerprint change, a behaviour change in a refactor, or risk larger
  than stated.

| Wave | Scope | Fingerprint |
|---|---|---|
| M0 | Contracts, spikes, ADR-005, deps | unchanged |
| M1 | `llm/config`: schema, loader, secrets, store, watcher, JSON Schema | unchanged |
| M2 | `llm/kinds` + catalog behind the existing seam (env → in-memory config); closes R-SOL-03, R-PLC-10 | **byte-identical** |
| M3 | Tasks registry, resolver, injected ModelPool, per-thread window/usage | byte-identical |
| M4 | Runtime switch: ModelSwitch, turn pinning, session column, history_portable, cache_control, subagent switch | one justified update |
| M5 | CLI: unconfigured boot, Setup Wizard, `models` subcommands, `/model` + subagent nudge | — |
| M6 | Daemon `/models` API + unconfigured boot; Electron wizard, Settings→Models, ModelChip; remove `.env` LLM writes | — |
| M7 | Env cutover: import, `LLM_*` ignored with warning, config_schema LLM group removed, `.env` key scrub offered | — |
| M8 | Agent `mdl_*` tools (out-of-band secrets) + user-presence gate on 3 OSes | profile matrix reviewed |
| M9 | Docs + live verification matrix | — |

**M0 — Contracts and spikes.**

- **Contract tests** in `test/framework_contract/`. They pin:
  - where deepagents places user middleware (`graph.py:672-728`);
  - the caching `isinstance` gate;
  - `ModelRequest.override(model=)` and per-call tool binding;
  - `ChatAnthropicVertex` parameters and `location="global"`;
  - `ChatBedrockConverse`'s `base_model`, and that it never writes `os.environ`;
  - the ModelProfile lookup keys;
  - the §2a cross-provider history matrix (raw fails where measured, portable is clean on all 45
    pairs);
  - genai on Vertex: retry on stream open, the Gemini 3 dummy signature, type-list conversion.
- **Spikes:**
  - Vertex MaaS through `ChatOpenAI` with an async token;
  - the summarizer decision (rule 12): **decided 2026-10-02**, ours owns it;
  - user presence on macOS (pyobjc LocalAuthentication), Windows (UserConsentVerifier, falling back to
    CredUI) and Linux (polkit `pkcheck`, falling back to PAM password). Each spike writes down its
    honest limits: the gate guards YUYUTSAVA's own surfaces, not the owner's file.
- **Dependencies:** declare `pydantic` and `tomlkit`, plus platform-marked presence deps.
- **ADR-005** covers the models config, its layers (global now, workspace later), the secrets model
  and RBAC as future work.
- **Exit:** contracts are green and each has been seen failing (negative control); the spike notes
  are committed; the fingerprint is unchanged.

**M1 — Config core.**

- schema, loader and friendly errors;
- `secrets.py` (0600 on POSIX, a user-only ACL on Windows through `platform/`);
- `store.py` (FileLock, atomic replace, generation check, comments kept);
- `watch.py` (thread, last good snapshot);
- `detect.py`;
- `schema_export.py`.
- **Exit:**
  - the config modules import in under 100 ms without LangChain (`scripts/verify_imports.py`);
  - `test_import_layers` is green;
  - every error path asserts its message *and* its fix text;
  - a round-trip test keeps comments byte-for-byte;
  - two concurrent writers: one wins, the other gets the stale error;
  - nothing mutates the environment (`test_tests_leave_the_environment_alone`).

**M2 — Kinds and catalog behind the existing seam.**

- Port the 11 provider types to `llm/kinds/`, with Connection plus auth objects.
- Vertex `gemini` moves to `ChatGoogleGenerativeAI(vertexai=True)` with every Gemini quirk carried
  over (rule 14).
- Add the Vertex `anthropic` and `maas` families and Bedrock auth methods (profile, keys,
  bedrock_api_key, `base_model` for application inference profiles).
- `discover()`/`verify()` and `live_price()` move onto each kind (**R-PLC-10**: no more `base_url`
  sniffing).
- `catalog.py` replaces the `max_input_tokens` ClassVars and the `context/config.py:29` lookup.
- `llm_settings_from_env` temporarily builds an in-memory config, so the 12 `chat_model` call sites
  don't move yet.
- Delete the settings classes and the `LlmSettings` Protocol (**R-SOL-03**). `disable_reasoning`
  becomes a capability-gated build option.
- **Exit:**
  - the fingerprint is byte-identical;
  - the hermetic suite is green;
  - real-SDK construction tests exist for every (type, family, auth method), **Bedrock included**;
  - discovery parsers are tested against recorded response fixtures;
  - `verify_imports --compare` is clean;
  - the lint ratchet is unchanged or lower.

**M3 — Selection.**

- `tasks.py`;
- the resolver Protocol in `ports/`;
- the ModelPool, built in bootstrap and the CLI composition roots;
- move every call site (`cli/cli.py:413,501`, `bootstrap.py:871-885,1121`,
  `conversation_manager.py:155`, `core/stores.py:403`, `core/async_host.py:67`,
  `core/model_router.py:117`) from env roles to tasks;
- compaction, meter and usage query per thread;
- usage rows record the provider type.
- **Exit:**
  - the fingerprint is byte-identical (fixture `models.toml` in the temp `YUYUTSAVA_HOME`);
  - `scripts/verify_loop_affinity.py` gains a pool check;
  - resolution runs clean under strict blockbuster;
  - a bad edit keeps the last good snapshot.

**M4 — Runtime switch.**

- `policy/model_switch.py` (unique `.name`, so `collapse_policy_adapters` keeps it);
- turn pinning;
- a session `model_ref` column (SQLite v4 in `storage/sessions/sqlite_impl.py`, PG migration 22 in
  `storage/pg/migrations.py`, a field on `storage/models.py:Session`);
- a subagent override map per thread;
- `history_portable`, plus the first-chunk `yuyutsava_model` stamp in every build wrapper;
- a parity test proving every Gemini quirk still applies on `ChatGoogleGenerativeAI(vertexai=True)`;
- `cache_control`;
- deepagents' summarizer is excluded for every provider type we build, and the overflow fallback
  moves into `YuyutsavaCompactionMiddleware` (rule 12);
- the harness `system_prompt_suffix` follows the active model (rule 13).
- **Exit:**
  - one deliberate fingerprint update, justified in the commit;
  - hermetic replay fixtures for Gemini↔Anthropic↔OpenAI-compatible histories, including tool loops
    and thinking blocks;
  - the switch takes effect on the next turn, never mid-loop.

**M5 — CLI.**

- the unconfigured path;
- `cli/setup_wizard/` on prompt_toolkit, themed;
- `cli/commands/models.py`, registered in `SUBCOMMANDS` (`cli.py:91`);
- `/model` and the subagent nudge in `chat_repl.py`, following the `/voice` and `/subagents` pattern
  (`:610-728`).
- **Exit:**
  - the CLI starts with no config, straight into the wizard;
  - wizard and picker tests run against a fake terminal;
  - `scripts/install_cli.py` is noted for the owner to re-run after the lock change.

**M6 — Daemon API and Electron.**

- `daemon/web/routers|schemas|services/models`, authenticated (`daemon/web/auth.py`), with change
  events;
- the daemon boots unconfigured;
- Electron gets `SetupWizard`, `ModelsSettings` and `ModelChip`;
- `electron-app/src/main/settings.js` stops writing LLM keys;
- `fallbackSchema.js` and `config_schema.py` lose the LLM group.
- **Exit:**
  - API tests;
  - renderer build and lint;
  - the flow checked in the Electron app over CDP (not the web app), leaving nothing behind.

**M7 — Env cutover.**

- `detect.py` import in both wizards;
- `LLM_*` env vars are ignored with a warning;
- an offer to remove imported keys from `.env`;
- `.env.example`, README and `docs/reference/configuration.md` are updated.
- **Exit:**
  - a test proves `LLM_*` is ignored and warned about;
  - no provider key is left in `os.environ` after the CLI or daemon starts, when keys come through
    `secret:` references.

**M8 — Agent tools and the presence gate.**

- `ToolFamily.MODELS` (`mdl_*`) in `core/agent_profiles.py`, wired in `_shared_master_tools`
  (`engine.py:539+`), on the chat deepagent only;
- out-of-band secret entry;
- `platform/user_presence.py`.
- **Exit:**
  - tests prove no secret value appears in checkpoints, `pending_asks` or interrupts;
  - `test_agent_profiles` passes with the unreviewed count at 0;
  - `test/platform` passes on macOS, Windows and Linux in CI (`.github/workflows/ci.yml:94-114`).

**M9 — Docs and live verification.**

- `docs/guides/models.md` is the single entry point. It has one copy-paste block per (provider ×
  auth method), plus "how to get this key" steps.
- `docs/reference/models-config.md` is generated from the JSON Schema.
- A CHANGELOG entry.
