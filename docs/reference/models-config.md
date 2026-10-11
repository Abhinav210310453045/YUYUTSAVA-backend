# `models.toml` reference

<!-- Generated from the schema by `yuyutsava models schema --markdown`. Do not edit:
     change the schema (yuyutsava/llm/config/schema.py) and regenerate. -->

Every setting `~/.yuyutsava/models.toml` accepts. For what to write and why — a
copy-paste block per provider and login — read the
[models guide](../guides/models.md). The loader has the last word: it also
checks what a schema cannot, such as a task naming a model the file defines.
`yuyutsava models doctor` checks a file without calling anyone.

## The file

| Setting | Value | | Meaning |
|---|---|---|---|
| `version` | `1` | required | The file's format: 1. |
| `defaults` | table | required | The model YUYUTSAVA uses unless told otherwise. |
| `tasks` | table | optional | A model per task: chat, orchestrator, triage, subagent, subagent.&lt;name&gt;, compaction, tier_light, tier_standard, tier_heavy. A task left out uses the default. |
| `providers` | table | required | Where models are served, and how to log in there. |
| `models` | table | optional | Short names for models, with their own settings. |
| `security` | table | optional | How YUYUTSAVA guards changes to your keys. |

## `[defaults]`

The model YUYUTSAVA uses unless told otherwise.

| Setting | Value | | Meaning |
|---|---|---|---|
| `model` | text | required | The model every task uses unless [tasks] gives it one: an alias or &lt;provider&gt;/&lt;model id&gt;. |

## `[tasks]`

A model per task: chat, orchestrator, triage, subagent, subagent.&lt;name&gt;, compaction, tier_light, tier_standard, tier_heavy. A task left out uses the default.

## `[providers.<name>]`

Where models are served, and how to log in there.

### `type = "vertex"`

Google Vertex AI: Gemini, Claude and open models in one Google Cloud project.

| Setting | Value | | Meaning |
|---|---|---|---|
| `project` | text | required | The Google Cloud project id. |
| `location` | text | required | Where requests are served: global (the most models) or a region such as us-central1. A model alias may override it. |

Logins — `auth = { method = … }`:

- `adc`: Google Application Default Credentials (``gcloud auth application-default login``).
- `service_account` — with `key_file = …`: A Google service-account key file.
- `access_token` — with `token = "secret:…"`: A Google OAuth access token, refreshed by whoever supplies it.

### `type = "google"`

Gemini through Google AI Studio.

Logins — `auth = { method = … }`:

- `api_key` — with `key = "secret:…"`: An API key, sent as the provider expects.

### `type = "bedrock"`

AWS Bedrock: every model family Bedrock serves, through the Converse API.

| Setting | Value | | Meaning |
|---|---|---|---|
| `region` | text | required | The AWS region, e.g. us-east-1. A model alias may override it. |

Logins — `auth = { method = … }`:

- `aws_default_chain`: Whatever the AWS SDK finds itself: environment, ``~/.aws``, an instance role.
- `aws_profile` — with `profile = …`: A named profile from ``~/.aws/config`` (SSO, assumed role, keys).
- `aws_keys` — with `access_key_id = "secret:…"`, `secret_access_key = "secret:…"`, `session_token = "secret:…"`: An access key pair, with a session token for temporary credentials.
- `bedrock_api_key` — with `key = "secret:…"`: A Bedrock API key, sent as a bearer token.

### `type = "anthropic"`

Claude through Anthropic's own API.

Logins — `auth = { method = … }`:

- `api_key` — with `key = "secret:…"`: An API key, sent as the provider expects.

### `type = "openai"`

OpenAI's API.

| Setting | Value | | Meaning |
|---|---|---|---|
| `base_url` | text | default `"https://api.openai.com/v1"` | Where the API is served. |

Logins — `auth = { method = … }`:

- `api_key` — with `key = "secret:…"`: An API key, sent as the provider expects.

### `type = "openrouter"`

OpenRouter: hundreds of models behind one key.

| Setting | Value | | Meaning |
|---|---|---|---|
| `base_url` | text | default `"https://openrouter.ai/api/v1"` | Where the API is served. |
| `http_referer` | text | optional | Optional: the site OpenRouter credits on its leaderboards. |
| `app_title` | text | optional | Optional: the app name OpenRouter shows on its leaderboards. |

Logins — `auth = { method = … }`:

- `api_key` — with `key = "secret:…"`: An API key, sent as the provider expects.

### `type = "groq"`

Groq's API.

| Setting | Value | | Meaning |
|---|---|---|---|
| `base_url` | text | default `"https://api.groq.com/openai/v1"` | Where the API is served. |

Logins — `auth = { method = … }`:

- `api_key` — with `key = "secret:…"`: An API key, sent as the provider expects.

### `type = "mistral"`

Mistral's API.

Logins — `auth = { method = … }`:

- `api_key` — with `key = "secret:…"`: An API key, sent as the provider expects.

### `type = "cohere"`

Cohere's API.

Logins — `auth = { method = … }`:

- `api_key` — with `key = "secret:…"`: An API key, sent as the provider expects.

### `type = "azure"`

Azure OpenAI: a model's ``id`` is the name of its deployment.

| Setting | Value | | Meaning |
|---|---|---|---|
| `endpoint` | text | required | The resource's endpoint: https://&lt;resource&gt;.openai.azure.com |
| `api_version` | text | default `"2024-10-21"` | The Azure OpenAI API version. |

Logins — `auth = { method = … }`:

- `api_key` — with `key = "secret:…"`: An API key, sent as the provider expects.

### `type = "openai_compatible"`

Any server that speaks OpenAI's chat API (xAI, DeepSeek, Together, a proxy, …).

| Setting | Value | | Meaning |
|---|---|---|---|
| `base_url` | text | required | The server's OpenAI API address, e.g. https://api.x.ai/v1 |

Logins — `auth = { method = … }`:

- `api_key` — with `key = "secret:…"`: An API key, sent as the provider expects.
- `bearer_token` — with `token = "secret:…"`: A token sent as ``Authorization: Bearer`` (a personal access token, a proxy's key).
- `none`: No credentials — a local server.

### `type = "ollama"`

An Ollama server, local by default.

| Setting | Value | | Meaning |
|---|---|---|---|
| `base_url` | text | default `"http://localhost:11434"` | Where the Ollama server listens. |

Logins — `auth = { method = … }`:

- `none`: No credentials — a local server.
- `bearer_token` — with `token = "secret:…"`: A token sent as ``Authorization: Bearer`` (a personal access token, a proxy's key).
- left out: `{method = "none"}`

## `[models.<alias>]`

A model under a short name, with anything that should differ from the provider's defaults.

| Setting | Value | | Meaning |
|---|---|---|---|
| `provider` | text | required | The [providers.&lt;name&gt;] that serves it. |
| `id` | text | required | The model id, as the provider names it. |
| `family` | `"gemini"` · `"anthropic"` · `"maas"` | optional | Vertex only: which family serves id. Inferred when left out: claude-* is anthropic, &lt;publisher&gt;/&lt;name&gt;-maas is maas, anything else gemini. |
| `location` | text | optional | Vertex only: a location for this model instead of the provider's. |
| `region` | text | optional | Bedrock only: a region for this model instead of the provider's. |
| `base_model` | text | optional | Bedrock and Azure only: the model behind id when id names an application inference profile (Bedrock) or a deployment (Azure) — what prices and limits are looked up by. |
| `context_window` | whole number | optional | The input window in tokens, when the known one is wrong or unknown. |
| `max_output` | whole number | optional | The most tokens one answer may have. |
| `thinking` | table | optional | For a thinking model: how many tokens it may think before answering. |
| `price` | table | optional | Its price, when the provider's own is not known or not wanted. |

### `thinking`

How many tokens a thinking model may spend before it answers.

| Setting | Value | | Meaning |
|---|---|---|---|
| `budget` | whole number | required | Tokens it may think for, on top of the answer's own; 0 turns thinking off. |

### `price`

US dollars per million tokens, when the provider's own price is not known or not wanted.

| Setting | Value | | Meaning |
|---|---|---|---|
| `input` | number | required | US dollars per million tokens sent to it. |
| `output` | number | required | US dollars per million tokens it answers with. |

## `[security]`

How YUYUTSAVA guards changes to your keys.

| Setting | Value | | Meaning |
|---|---|---|---|
| `require_user_presence` | true · false | default `false` | Have the OS confirm it is you (Touch ID or your password, Windows Hello, polkit) before YUYUTSAVA stores or removes a key, or turns this off. |
