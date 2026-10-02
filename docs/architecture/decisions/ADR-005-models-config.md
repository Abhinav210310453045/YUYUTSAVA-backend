# ADR-005 — One models config file, every provider, a model chosen per task and per conversation

**Status:** Accepted by the owner on 2026-10-02. Execution is tracked in [the design](../../design/model-config.md#7-waves).
**Addresses:** [`R-SOL-03`](../../code-review/v0.1.0/06-solid-dry.md), [`R-PLC-10`](../../code-review/v0.1.0/04-placement-and-layering.md)
**Requires:** ADR-003 (composition root), ADR-004 (`ModelHandle`, framework boundary)

---

## Context

The model layer is configured only through the environment.

- **One model per role.** `llm_settings_from_env(role)` reads `LLM_PROVIDER` and
  `<ROLE>_LLM_PROVIDER` once, when a process starts. It yields one provider and one model per role.
  Changing either means editing an env file and restarting.
- **No live selection.** No surface — CLI, daemon API or app — can list models, verify a login, or
  switch while running.
- **The context window belongs to the wrong thing.** It is a `ClassVar` on each provider's settings
  class, so a provider that serves a 128k model and a 1M model reports one number for both.
- **Logins are fixed per provider type.** Vertex can only use ADC, and Bedrock only the default boto3
  chain. A service-account file, an AWS profile or a Bedrock API key can't be named. Two accounts on
  the same provider can't coexist.
- **Most of each cloud is unreachable.** Vertex serves Claude and open models (Model Garden) besides
  Gemini, and Bedrock serves many model families. None of them can be selected.
- **Settings are written and read inconsistently.** The Electron app writes provider keys to
  `~/.yuyutsava/.env`, which the daemon reads and the CLI does not.

Making the product adoptable needs more than another provider class:

- a single, documented place to declare providers, logins and models;
- a way for non-technical users to fill it in;
- the ability to change the model mid-conversation.

## Decision

### 1. `~/.yuyutsava/models.toml` is the only source of model configuration

It has four tables:

| Table | Holds |
|---|---|
| `providers` | Where to call and how to log in |
| `models` | Aliases and per-model overrides |
| `defaults.model` | The model every task uses unless it has its own |
| `tasks` | Per-task picks from a closed registry, including `subagent.<name>` |

The rest of the design follows from that file:

- **Model references.** A model is referenced by an alias or by `<provider>/<model-id>`.
- **Validated with fixes.** The file is validated against a schema that is also published as JSON
  Schema. Every error names the path, the problem and the fix.
- **Writers.** It is written by the CLI Setup Wizard, the Electron Setup Wizard and Settings page,
  agent tools, and by hand. Every program writes through one Python store that preserves comments,
  holds a file lock and rejects a write based on a stale read.
- **Read live.** A watcher thread validates each edit and publishes an immutable snapshot. An invalid
  edit leaves the last good snapshot in force and is reported.
- **Env vars.** After a one-time import offered by the wizards, `LLM_*` environment variables are
  ignored, with a warning that names the file.

### 2. Secrets are references, never values

The file holds `secret:<name>`, `env:NAME` or `file:path`.

- **Storage.** `secret:` values live in `~/.yuyutsava/secrets.toml`, readable only by its owner. This
  is the same trust model as `~/.aws/credentials`, and it works the same on every OS and in
  containers.
- **Presence gate.** An opt-in OS user-presence check guards edits and reveals made through
  YUYUTSAVA's own surfaces: Touch ID or password on macOS, Windows Hello, polkit or password on Linux.
  It is not encryption at rest, and it does not claim to be.
- **Agent tools.** These ask for a secret by name. Its value is written straight to the store by the
  CLI or the app, and it never passes through a checkpoint or interrupt payload.

### 3. A provider type owns everything about reaching it

Each type declares:

- its connection fields;
- the login methods it accepts;
- its model families;
- how to build a chat model;
- how to discover models and verify a login (free endpoints only);
- how to look up prices.

Credentials are passed to SDKs as objects, never through `os.environ`. Adding a provider remains one
registry entry.

### 4. Model facts come from a catalog, not from the provider type

The context window, output limit, capabilities and price for each model are resolved from these
sources, highest first:

1. a `models.toml` override;
2. the discovery cache;
3. LangChain's model profile;
4. the provider type's default.

The window follows the model actually in use.

### 5. Selection is resolved per call and pinned per turn

A middleware replaces the request's model on each call. The agent graph is not rebuilt when the
model changes. Rules:

- **Turn pinning.** The choice is fixed when the human message arrives. A switch or edit takes effect
  on the next turn, never inside a tool loop.
- **Per-thread choice.** A conversation's choice is stored on its session.
- **Subagents.** Subagents keep their own task setting unless the user applies the switch to them.

**History portability.** An assistant message is sent unchanged only to the model that wrote it. Any
other model receives its text and tool calls alone. This rule was measured, not assumed: across five
producers and nine targets, only that form was accepted by every SDK's request builder (see the
design, §2a).

### 6. Scope: global now, layered by design

The configuration is global. A later workspace layer (`<workspace>/.yuyutsava/models.toml`, trusted
workspaces only, as for MCP) would be able to override `defaults` and `tasks` but never `providers`
or secrets.

Role-based access control would then decide who may add providers or change assignments at each
layer. Neither the workspace layer nor RBAC exists in code: the loader handles one layer, and adding
one is a data change to the loader's input, not a new mechanism.

## Alternatives considered

**Keep the environment as the configuration.** Env vars can't hold a list of providers, per-model
overrides or a per-conversation choice. They can't be edited safely by an app, and they leak into
every child process. Rejected.

**Store the configuration in the database.** A database makes the UI simple, but the file could no
longer be read, versioned, copied between machines or documented as one artifact. Config would also
depend on a database being up. Rejected; the database keeps only the per-session choice.

**YAML or JSON instead of TOML.**

- **YAML:** indentation errors are the most common failure for non-technical editors.
- **JSON:** has no comments, so the file can't explain itself.

TOML is read by the standard library, and `tomlkit` writes it without losing comments.

**LangChain `init_chat_model` as the factory.** It infers the legacy `bedrock` class for Bedrock ids,
needs packages that aren't installed for Groq and OpenRouter, and knows nothing about logins,
quirks, discovery or our capabilities. Rejected; provider types call SDK classes directly, as today.

**LangChain's Vertex MaaS classes for open models on Vertex.** They accept a fixed list of models,
build an invalid host for `location="global"`, and keep a token fetched once at construction until it
expires. Rejected in favour of `ChatOpenAI` against Vertex's OpenAI-compatible endpoint, with a
refreshing token.

**Keep `ChatVertexAI` for Gemini on Vertex.** It is deprecated in langchain-google-vertexai 3.2.0
and removed in 4.0, misses a 429/503 that surfaces on a stream's first read, and lacks the Gemini 3
thought-signature patch. Rejected for `ChatGoogleGenerativeAI(vertexai=True)`, which was verified
against all three locally. Every Gemini fix we carry is kept on the new path and re-validated there.

**Rebuild the agent graph on every model switch.** This costs a full build per switch and loses the
per-thread choice in the daemon, where one bundle serves many threads. Rejected for a per-call model
override.

**LangChain standard content blocks as the cross-provider history format.** Measured as not
universal: Anthropic and Bedrock histories still fail the Vertex and Bedrock request builders.
Rejected for text and tool calls only.

**OS keychain as the secret store.** It is stronger at rest, but it fails on headless Linux and in
containers, and Electron and Python would have to share it. Deferred: a keychain backend can sit
behind the same `secret:` reference later without changing any file.

## Consequences

**Positive.**

- One file, one schema and one page of documentation cover every provider and login.
- Users can switch models mid-conversation from any surface, and each task can use the model that
  suits it.
- Claude and open models on Vertex, and every Bedrock family, become reachable. So do several
  accounts per provider type.
- The context window and price follow the active model. Usage rows record the provider, so the same
  model name on two providers is priced correctly.
- Closes `R-SOL-03` (settings that fake `api_key`/`base_url`) and `R-PLC-10` (provider detection from
  `base_url` substrings).

**Negative.**

- `pydantic` and `tomlkit` become declared dependencies. The presence gate adds platform-marked ones.
- A switched conversation loses the previous model's private reasoning from earlier turns. Only text
  and tool calls carry over. This is inherent: no provider accepts another's reasoning.
- The daemon and CLI must be able to start with no configuration, which adds a "setup required" state
  to both.
- Discovery and login checks call external endpoints, which are free but still network calls. A test
  message is billable and is only sent on an explicit request.

## Verification

- Contract tests in `test/framework_contract/` pin every SDK behaviour this design relies on:
  - middleware placement;
  - the prompt-caching gate;
  - per-call model override;
  - Vertex Claude parameters;
  - Bedrock credential handling;
  - the cross-provider history matrix.
- The agent fingerprint stays byte-identical through the refactoring waves, and changes exactly once,
  deliberately, when the model switch is added.
- Live runs, one per provider and family plus one cross-provider conversation, are marked `billable`
  and run only with the owner's approval.
