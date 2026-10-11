# Connecting models

YUYUTSAVA works with any of twelve kinds of model provider — Google (Vertex AI
or AI Studio), AWS Bedrock, Anthropic, OpenAI, Azure OpenAI, OpenRouter, Groq,
Mistral, Cohere, Ollama on your own machine, or any server that speaks OpenAI's
API. Which ones it uses, how it logs in to each, and which model does which job
all live in one file: **`~/.yuyutsava/models.toml`**.

You never have to write that file yourself. This guide shows the quick way, then
what the file says, then a ready-to-paste block for every provider and every way
of logging in to it.

- [The quick way](#the-quick-way)
- [What the file says](#what-the-file-says)
- [Each provider](#each-provider)
- [Switching models](#switching-models)
- [Keys](#keys)
- [When something is wrong](#when-something-is-wrong)
- [From `.env` to `models.toml`](#from-env-to-modelstoml)
- [Every command](#every-command)

Every setting, with its type and default: the
[`models.toml` reference](../reference/models-config.md).

---

## The quick way

```bash
yuyutsava models setup
```

The setup wizard asks a few questions, one at a time:

1. **What this machine already has** — a gcloud login, AWS profiles, a running
   Ollama, API keys in your environment, the setup your `.env` describes. Pick
   what to use.
2. **Anything else, by hand** — the provider, then only what that provider
   needs, then how you log in (only the ways it accepts, each in plain words).
3. **A login check** — free: YUYUTSAVA asks the provider which models your
   account can use. If it fails, you are told why and how to fix it.
4. **The default model**, from the ones ready to use.
5. **The tasks** — every job uses the default, unless you want some on another
   model.

Nothing is written until the end. The **desktop app** has the same wizard: it
opens by itself when no model is set up, and from **Settings → Models**. The
first chat on a machine without the file offers it too.

---

## What the file says

```toml
version = 1

[defaults]
model = "flash"                     # every job uses this unless [tasks] says otherwise

[tasks]                             # optional
triage = "local/llama3.2:3b"        # sorting incoming events: a small local model is plenty

[providers.google]                  # WHERE models are served, and how to log in there
type     = "vertex"
project  = "my-project"
location = "global"
auth     = { method = "adc" }

[providers.local]
type = "ollama"

[models.flash]                      # a short name for a model, and its own settings
provider = "google"
id       = "gemini-3.5-flash"
thinking = { budget = 8192 }
```

- **`[providers.<name>]`** — a place models come from. The name is yours
  (`google`, `work`, `local`); `type` says which kind of provider it is.
- **A model** is named either by an alias from `[models]` (`flash`) or as
  `<provider>/<model id>` (`local/llama3.2:3b`). The full form needs no
  `[models]` entry; an alias exists to give a model a short name or its own
  settings — a location, a context window, a thinking budget, a price.
- **`[tasks]`** — the jobs a model does: `chat` (the CLI, the app's chat and
  voice), `orchestrator` (background tasks and Tinker), `triage` (incoming
  events), `subagent` (and `subagent.<name>` for one of them), `compaction`
  (summaries of long conversations), and `tier_light` / `tier_standard` /
  `tier_heavy` when smart routing is on. A job you leave out uses the default.

A running YUYUTSAVA reads a change the moment you save it, and uses it from the
next message — no restart. A change that does not load is reported (in the CLI,
the app, the daemon's log and `yuyutsava models doctor`), and the last good file
stays in use meanwhile.

---

## Each provider

Each block below goes into `models.toml` as it is; add a default that uses it
(`yuyutsava models use <provider>/<model id>`) if it is your first provider.
Model ids are examples — `yuyutsava models discover <provider>` lists the ones
your account can use, marked ✓ when ready.

A key is never written into the file. `secret:<name>` points into
`~/.yuyutsava/secrets.toml`, which only you can read; store a key there with
`yuyutsava models secrets set <name>` (it asks for the value without showing
it). See [Keys](#keys) for the other ways.

### Google Vertex AI — `type = "vertex"`

Gemini, Claude and open models (Llama, gpt-oss, …) in your Google Cloud
project. You need a project with the Vertex AI API turned on:
[console.cloud.google.com → APIs → Vertex AI API → Enable](https://console.cloud.google.com/apis/library/aiplatform.googleapis.com).
`location = "global"` serves the most models; a single model can run elsewhere
with its own `location`.

**With your gcloud login** — run `gcloud auth application-default login` once:

```toml
[providers.google]
type     = "vertex"
project  = "my-project"
location = "global"
auth     = { method = "adc" }
```

**With a service account** — Console → IAM & Admin → Service accounts → create
one with the role *Vertex AI User* → Keys → Add key → JSON:

```toml
[providers.google-sa]
type     = "vertex"
project  = "my-project"
location = "global"
auth     = { method = "service_account", key_file = "~/keys/yuyutsava-sa.json" }
```

**With an access token** — `gcloud auth print-access-token`; it expires in
about an hour, so this is for trying things out:

```toml
[providers.google-token]
type     = "vertex"
project  = "my-project"
location = "global"
auth     = { method = "access_token", token = "secret:google-token" }
```

Claude and open models on Vertex, each with its own location:

```toml
[providers.google]
type     = "vertex"
project  = "my-project"
location = "us-central1"
auth     = { method = "adc" }

[models.sonnet]
provider = "google"
id       = "claude-sonnet-4-6"
location = "global"

[models.gpt-oss]
provider = "google"
id       = "openai/gpt-oss-120b-maas"
location = "global"
```

Claude on Vertex needs it enabled in Model Garden and a quota above zero; until
then the login check marks it ⊘ with the link to ask for one.

### Google AI Studio — `type = "google"`

Gemini with a key — the quickest Google start. Get one at
[aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey):

```toml
[providers.gemini]
type = "google"
auth = { method = "api_key", key = "secret:gemini" }
```

### AWS Bedrock — `type = "bedrock"`

Every model family Bedrock serves. A model can run in another region with its
own `region`; an application inference profile goes in `id`, with the model
behind it as `base_model` so prices and limits are known. If a model needs
access first, the login check marks it ⊘; ask for it in the Bedrock console.

**With whatever AWS finds by itself** (environment variables, `~/.aws`, an
instance role):

```toml
[providers.aws]
type   = "bedrock"
region = "us-east-1"
auth   = { method = "aws_default_chain" }
```

**With a profile** from `~/.aws/config` — `aws configure`, or
`aws configure sso` for single sign-on:

```toml
[providers.aws-work]
type   = "bedrock"
region = "us-east-1"
auth   = { method = "aws_profile", profile = "work" }
```

**With an access key pair** — IAM → Users → Security credentials → Create
access key:

```toml
[providers.aws-keys]
type   = "bedrock"
region = "us-east-1"
auth   = { method = "aws_keys", access_key_id = "secret:aws-id", secret_access_key = "secret:aws-secret" }
```

Temporary credentials add `session_token = "secret:aws-session"`.

**With a Bedrock API key** — Bedrock console → API keys:

```toml
[providers.aws-key]
type   = "bedrock"
region = "us-east-1"
auth   = { method = "bedrock_api_key", key = "secret:bedrock" }

[models.sonnet-bedrock]
provider = "aws-key"
id       = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
```

### Anthropic — `type = "anthropic"`

Claude with an Anthropic key, from
[console.anthropic.com → API keys](https://console.anthropic.com/settings/keys):

```toml
[providers.anthropic]
type = "anthropic"
auth = { method = "api_key", key = "secret:anthropic" }
```

### OpenAI — `type = "openai"`

From [platform.openai.com/api-keys](https://platform.openai.com/api-keys):

```toml
[providers.openai]
type = "openai"
auth = { method = "api_key", key = "secret:openai" }
```

### Azure OpenAI — `type = "azure"`

OpenAI models deployed in your Azure resource. The endpoint and a key are under
your resource → *Keys and Endpoint*; a model's `id` is the name of its
**deployment**, with the model behind it as `base_model`:

```toml
[providers.azure]
type     = "azure"
endpoint = "https://my-resource.openai.azure.com"
auth     = { method = "api_key", key = "secret:azure" }

[models.gpt-azure]
provider   = "azure"
id         = "my-gpt-deployment"
base_model = "gpt-4o-mini"
```

### OpenRouter — `type = "openrouter"`

Hundreds of models behind one key, from
[openrouter.ai/settings/keys](https://openrouter.ai/settings/keys). Model ids
carry their maker: `openrouter/anthropic/claude-sonnet-4.5`.

```toml
[providers.openrouter]
type = "openrouter"
auth = { method = "api_key", key = "secret:openrouter" }
```

### Groq — `type = "groq"`

Fast open models, with a free tier. Key from
[console.groq.com/keys](https://console.groq.com/keys):

```toml
[providers.groq]
type = "groq"
auth = { method = "api_key", key = "secret:groq" }
```

### Mistral — `type = "mistral"`

Key from [console.mistral.ai/api-keys](https://console.mistral.ai/api-keys/):

```toml
[providers.mistral]
type = "mistral"
auth = { method = "api_key", key = "secret:mistral" }
```

### Cohere — `type = "cohere"`

Key from [dashboard.cohere.com/api-keys](https://dashboard.cohere.com/api-keys):

```toml
[providers.cohere]
type = "cohere"
auth = { method = "api_key", key = "secret:cohere" }
```

### Ollama — `type = "ollama"`

Models on your own machine: free and private. Install it from
[ollama.com](https://ollama.com), then `ollama pull llama3.2:3b`:

```toml
[providers.local]
type = "ollama"
```

An Ollama server elsewhere, behind a token:

```toml
[providers.gpu-box]
type     = "ollama"
base_url = "https://ollama.example.com"
auth     = { method = "bearer_token", token = "secret:gpu-box" }
```

### Any OpenAI-compatible server — `type = "openai_compatible"`

xAI (`https://api.x.ai/v1`), DeepSeek (`https://api.deepseek.com/v1`), Together
(`https://api.together.xyz/v1`), Fireworks, a proxy of your own — anything that
speaks OpenAI's chat API.

```toml
[providers.xai]
type     = "openai_compatible"
base_url = "https://api.x.ai/v1"
auth     = { method = "api_key", key = "secret:xai" }
```

A server that takes a bearer token, or none at all:

```toml
[providers.my-proxy]
type     = "openai_compatible"
base_url = "https://llm.internal.example.com/v1"
auth     = { method = "bearer_token", token = "secret:my-proxy" }

[providers.lan-server]
type     = "openai_compatible"
base_url = "http://192.168.1.20:8000/v1"
auth     = { method = "none" }
```

---

## Switching models

**One conversation.** In a CLI chat, `/model` lists the models with numbers;
`/model 2` or `/model sonnet` moves this conversation to it from your next
message, never in the middle of an answer. `/model reset` puts it back on what
`models.toml` says. In the app, the chip above the message box does the same.
The choice belongs to the conversation: resume it later, here or in the app,
and it is still on that model.

**Its subagents.** After a switch, if the subagents still use another model,
you are offered to move them too — `/model subagents all`, or one by name with
`/model subagents <name>`; in the app, one click.

**Every conversation.** `yuyutsava models use sonnet` (or `/model default
sonnet`, or Settings → Models) changes the default. To give one job its own
model: `yuyutsava models use local/llama3.2:3b --task triage`; to put it back
on the default: `yuyutsava models follow triage`.

A conversation can move between providers freely — Gemini, then Claude, then a
local model, with tool calls in between — because each model is sent the
earlier turns in a form every provider accepts.

---

## Keys

A key is written as a reference, never as itself:

| Reference | Where the key is |
|---|---|
| `secret:<name>` | `~/.yuyutsava/secrets.toml` — readable by you alone; `yuyutsava models secrets set <name>` |
| `env:VARIABLE` | an environment variable — e.g. `env:GROQ_API_KEY` |
| `file:<path>` | a file holding just the key |

`yuyutsava models secrets list` shows the names stored, never the values;
`yuyutsava models secrets rm <name>` removes one.

**Asking for you first.** `yuyutsava models security on` (or Settings → Models
→ Security) makes YUYUTSAVA ask the computer to confirm it is you — Touch ID or
your password on a Mac, Windows Hello on Windows, a polkit dialog on a Linux
desktop — before it stores or removes a key, or turns this off again. One
confirmation lasts five minutes. It guards YUYUTSAVA's own ways in (the
wizards, `yuyutsava models`, the app), not the file itself. A computer with none
of those dialogs cannot turn it on.

---

## When something is wrong

Start with **`yuyutsava models doctor`**: it checks the file and every key it
names, without calling anyone, and says what each job will use.
**`yuyutsava models verify`** logs in to each provider (free) and says what it
found.

What the marks next to a model mean:

| Mark | Meaning | What to do |
|---|---|---|
| ✓ | ready | — |
| ⊘ | your account cannot use it yet: no quota, or access not granted | follow the link given — ask for a quota increase, or for access to the model |
| ⌖ | not served where your provider points | give the model its own `location` (Vertex) or `region` (Bedrock): `yuyutsava models alias sonnet google/claude-sonnet-4-6 --location global` |
| ? | the provider did not say | try it: `yuyutsava models verify --test-message <model>` sends one short, billable message |

Messages you may see in a chat:

- **"no quota for … — request an increase"** — the account's quota for that
  model is zero (on Vertex, Claude starts that way). Resending does not help;
  the message carries the link to ask for more, or switch model.
- **"provider busy (…) · retry 1/…"** — the provider is overloaded for a
  moment, and YUYUTSAVA retries by itself. If the retries give up, resend.
- **"the provider does not serve … where it was asked for"** — the model is
  not served in that location or region; give it its own (see ⌖ above).
- **A login that fails** in the wizard, `verify` or Settings is said with its
  fix: an expired gcloud login (run `gcloud auth application-default login`
  again), a key that is wrong or missing (`yuyutsava models secrets set
  <name>`), a project without the Vertex AI API turned on.

If the **app** shows the setup wizard instead of the chat, YUYUTSAVA has no
model it can use; finishing the wizard starts it.

---

## From `.env` to `models.toml`

Before `models.toml` exists, YUYUTSAVA takes its model from the environment:
`LLM_PROVIDER` and that provider's variables (see `.env.example`, section 1).
The setup wizard offers to bring that setup over, keys included (copied into
`secrets.toml`).

Once `models.toml` exists, those variables are **not read**. YUYUTSAVA says so
when it starts and in `yuyutsava models doctor`, naming each one still set and
the file it is in; **`yuyutsava models clean-env`** (or the button in Settings
→ Models) removes them from that file and keeps everything else in it. A key
`models.toml` still reads through `env:` is left alone, and so is
`GROQ_API_KEY`, which voice speech-to-text reads.

---

## Every command

| Command | What it does |
|---|---|
| `yuyutsava models setup` | the setup wizard |
| `yuyutsava models add` | one more provider, step by step |
| `yuyutsava models list` | providers, models, the default and each job's model |
| `yuyutsava models doctor` | check the file and its keys, calling no one |
| `yuyutsava models discover [<provider>…]` | the models each provider offers your account (free) |
| `yuyutsava models verify [--test-message <model>]` | log in to each provider (free); `--test-message` sends one billable message |
| `yuyutsava models use <model> [--task <task>]` | the default, or a job's model |
| `yuyutsava models follow <task>…` | jobs back on the default |
| `yuyutsava models alias <name> <provider>/<id> [--location … \| --region … \| --context-window …]` | a short name for a model, with its settings |
| `yuyutsava models remove <name>` | a provider or an alias |
| `yuyutsava models secrets set\|rm\|list [<name>]` | the keys in `secrets.toml` |
| `yuyutsava models security [on\|off]` | asking for you before a key changes |
| `yuyutsava models clean-env [--yes]` | remove model variables `models.toml` replaced from `.env` |
| `yuyutsava models edit` | open the file in your editor, then check it |
| `yuyutsava models path` | where the files are |
| `yuyutsava models schema [--markdown]` | the JSON Schema (for editors), or the reference page |
