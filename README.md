# YUYUTSAVA

A local-first AI agent runtime. It runs on your machine, works on your files
with your tools, and keeps its state in your own database.

It has two operating modes that share the same agent stack:

- **CLI** — a one-shot or interactive agent in your terminal. `yuyutsava "…"`,
  and it reads, writes, and runs things in a workspace you nominate.
- **Daemon** — an always-on background process with a desktop app. It watches
  events (files, clipboard, hotkeys, app focus), routes work to specialist
  subagents, talks over voice, and persists everything it does.

Built on [LangGraph](https://github.com/langchain-ai/langgraph) and
[deepagents](https://github.com/langchain-ai/deepagents), with a permission
layer between the model and your filesystem.

> **Status: alpha.** It is used daily by its author, and the internals still
> move. Interfaces are not yet stable. See
> [docs/code-review/](docs/code-review/) for a candid assessment of the
> codebase's structure, what was fixed, and what is still carried.

<!-- TODO: screenshot of the Electron app goes here -->

---

## Requirements

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** (recommended) or pip
- **Node 18+** — only for the desktop app
- **Docker** — only for the sandboxed execution mode
- **PostgreSQL 15+ with pgvector** — only for durable/semantic-memory mode
  (SQLite is the zero-config default)

---

## Quick start

```bash
git clone https://github.com/Abhinav210310453045/YUYUTSAVA-backend.git
cd YUYUTSAVA-backend
uv sync

uv run yuyutsava models setup   # connect a model provider, step by step
```

Run a task:

```bash
uv run yuyutsava "list every Python file in this repo and count the lines"
```

The first chat offers the same setup if you skip it. The desktop app has its
own setup wizard and opens it whenever no model is set up.

`.env.example` is the reference for everything else — 16 numbered sections,
each stating its own defaults, all of it optional.

### Installing the CLI

`uv run yuyutsava …` from the checkout — and the desktop app, which starts the
daemon that way — runs on the locked dependencies (`uv.lock`). To have a
`yuyutsava` command on your PATH that does the same, install it with:

```bash
python scripts/install_cli.py --extra vertex   # the extras you use; repeatable
```

It installs the checkout editable, constrained to `uv.lock`. Do not use
`pipx install -e .` or a bare `uv tool install -e .`: they resolve the newest
versions the ranges allow, which the tests and contracts never ran (an
unlocked install once took every MCP server down). If you have one, run
`pipx uninstall yuyutsava` first. The daemon logs its dependency versions at
start-up and warns when they are not the locked ones.

### Choosing models

Which providers, logins and models YUYUTSAVA uses lives in one file,
`~/.yuyutsava/models.toml`. Fill it with the setup wizard (`yuyutsava models
setup`, or the desktop app), by hand, or a bit of both — a running YUYUTSAVA
picks up a change without a restart:

```toml
version = 1

[defaults]
model = "gemini-flash"               # every task without its own model uses this

[tasks]                              # optional: cheap work on a cheap model
triage = "local/llama3.2:3b"         # <provider>/<model id> needs no alias

[providers.google]
type     = "vertex"
project  = "my-project"
location = "asia-south1"
auth     = { method = "adc" }        # `gcloud auth application-default login`

[providers.local]
type = "ollama"

[models.gemini-flash]                # a short name, and settings for one model
provider = "google"
id       = "gemini-3.5-flash"
```

Keys never go in it: write `secret:<name>` (kept in `~/.yuyutsava/secrets.toml`,
readable by you alone — `yuyutsava models secrets set <name>`), `env:VARIABLE`
or `file:<path>`. `yuyutsava models security on` makes changing a key ask for
Touch ID or your password first (Windows Hello on Windows, polkit on Linux).

| Provider types | |
|---|---|
| OpenAI-compatible (no extra install) | `groq` · `openrouter` · `ollama` · `openai` · `openai_compatible` |
| Native SDK (`uv sync --extra <name>`) | `anthropic` · `google` · `vertex` · `bedrock` · `azure` · `mistral` · `cohere` |

`openai_compatible` plus a base URL covers xAI, DeepSeek, Together, Fireworks,
Perplexity, Cerebras, DeepInfra and anything else speaking that API. `ollama`
runs fully local with no key.

`yuyutsava models` lists, checks and changes it: `list`, `doctor`, `discover`,
`verify`, `use <model> [--task …]`, `alias`, `edit`, `schema`, and more
(`yuyutsava models --help`). In a chat, `/model` — or the model chip in the
app — moves that conversation to another model from its next message.

The whole story — a ready-to-paste block for every provider and every way of
logging in, switching, keys, and what to do when something is wrong — is in
**[docs/guides/models.md](docs/guides/models.md)**.

**Without `models.toml`**, models still come from the environment, as before:
`LLM_PROVIDER` and that provider's variables (`.env.example`, section 1). Once
the file exists those are not read; YUYUTSAVA names any still set, and
`yuyutsava models clean-env` removes them from the `.env` they are in.

---

## The CLI

```bash
yuyutsava "your task"                 # one-shot
yuyutsava chat                        # interactive REPL
yuyutsava --continue "and now …"      # resume the latest session here
yuyutsava --resume <id> "…"           # resume a specific session
yuyutsava --list-sessions             # what's persisted
```

Useful flags: `--workspace/-w` (the root the agent may touch, default cwd),
`--verbose/-v`, `--execution docker`, `--print-tools`, `--list-scenarios`.
Full list via `yuyutsava --help`.

### Sandboxed execution

Tools can run inside a container instead of on the host:

```bash
docker build -t yuyutsava-sandbox:local -f yuyutsava/docker_sandbox/Dockerfile .

yuyutsava --execution docker \
          --docker-image yuyutsava-sandbox:local \
          --docker-network none \
          "generate a report and save it to the deliverables dir"
```

`--docker-memory`, `--docker-cpus` and `--docker-pids-limit` bound the
container; `--docker-network none` removes outbound network.

---

## The daemon and desktop app

```bash
uv run yuyutsava daemon               # add --verbose to watch it work
uv run yuyutsava daemon --status      # PID, URLs, uptime
uv run yuyutsava daemon --stop
```

It serves on `http://127.0.0.1:7654`. Loopback binds skip auth; binding
elsewhere requires `YUYUTSAVA_API_TOKEN`.

> Do **not** pass `--no-ui` if you want the desktop app — that flag is headless
> mode and shuts off the web API the app connects to. To serve the API without
> opening the app (how the app itself starts the daemon), pass `--no-app`.

The desktop app is an Electron client in [`electron-app/`](electron-app/):

```bash
cd electron-app
npm install
npm run dev       # Vite + Electron together
npm run dist      # package a distributable
```

Other subcommands — these are dispatched before argument parsing, so they do
**not** appear in `yuyutsava --help`:

| Command | Purpose |
|---|---|
| `yuyutsava chat` | Interactive REPL |
| `yuyutsava daemon` | Run the background daemon |
| `yuyutsava attach` | Attach a terminal to a running daemon's session |
| `yuyutsava prefs {list\|get\|set\|delete}` | Read/write user preference rows |

---

## What it does

**Agent core** — a task-runner gateway that mediates every filesystem and shell
call through a zone/permission model, with a tiered prompt/auto-approve policy
you configure rather than patch.

**Background work** — long jobs run as detached subagents; completion wakes the
orchestrator on the parent thread rather than blocking a turn.

**Event-driven triage** — filesystem, clipboard, hotkey and app-focus sources
feed a bus; a cheap triage model decides what deserves the expensive one.

**Voice** — speech in, speech out, over the same WebSocket the text chat uses.
Wake word, VAD, barge-in. Providers are configurable (faster-whisper/Groq for
STT, Piper/ElevenLabs for TTS), with a zero-config macOS `say` fallback.

**TODO board** — a persistent planning surface with a dedicated agent that
works cards, attaches artifacts, and delegates.

**Memory and retrieval** — pgvector-backed semantic memory and skill recall,
with automatic context compaction and tool-result offloading.

**MCP** — connects to Model Context Protocol servers (stdio or SSE), scoped per
agent so each subagent sees only the tools it should, hot-reloadable on
`SIGHUP`.

**Visuals** — charts, styled tables, syntax-highlighted code, math and diagrams
rendered to images the agent can hand back.

**Storage** — SQLite by default, PostgreSQL when you want durability and
semantic search, behind one dialect layer.

---

## Configuration

Optional files in `~/.yuyutsava/` (override with `YUYUTSAVA_HOME`):

| File | Controls |
|---|---|
| `models.toml` | Providers, logins, the default model and each task's ([Choosing models](#choosing-models)); its keys in `secrets.toml` |
| `mcp_config.json` | Which MCP servers start; which agents see their tools. A trusted workspace can add its own in `<workspace>/.yuyutsava/mcp_config.json` |
| `permissions.json` | Which tool calls skip the prompt; daily caps |
| `events_config.json` | Which event sources run, and their tuning |

Inside each workspace, everything yuyutsava writes lives under
`<workspace>/.yuyutsava/` — scratch sandbox, reusable scripts, deliverables,
`ChangeLog.md` / `Assumptions.md` / `workspace_memory.md`, workspace skills
and an optional workspace-level `mcp_config.json`. It carries its own `*`
`.gitignore`, so none of it is ever committed. In Docker mode that directory
is the container's only writable mount.

Full schemas and examples: **[docs/reference/configuration.md](docs/reference/configuration.md)**.

---

## Documentation

Start at **[docs/](docs/README.md)**.

| | |
|---|---|
| [Architecture overview](docs/architecture/overview.md) | The whole system, both modes, all subsystems |
| [Daemon flows](docs/architecture/daemon.md) | Boot, events, triage, orchestration, shutdown |
| [Transport](docs/architecture/transport.md) | Wire level — SSE/WebSocket frames, the voice PCM path |
| [`/v1` API](docs/reference/api-v1.md) | The daemon's HTTP contract |
| [Code review](docs/code-review/) | Versioned reviews: findings, metrics, remediation and what is carried |
| [Architecture decisions](docs/architecture/decisions/) | The ADRs behind today's structure |

---

## Project layout

```
yuyutsava/
  core/          agent construction, engine, streaming, config
  llm/           provider layer — one module per provider, plus quirks
  cli/           terminal entry point and rich REPL
  daemon/        always-on process: web server, orchestrator, triage
  agents/        task runner, orchestrator, tinker, subagents
  ports/         dependency-free protocols (the acyclic layer)
  policy/        cross-cutting policy in our own types, not the framework's
  storage/       SQLite/Postgres behind one dialect adapter
  context/       compaction and tool-result offloading
  memory/        semantic long-term memory (pgvector)
  retrieval/     shared retrieval base for memory and skills
  events/        bus, store, sources, registry
  mcp/           MCP client manager, tool adapter, scoping
  todoboard/     planning surface, cards, artifact blocks
  artifacts/     non-card artifact store for chat and voice
  voice/         STT/TTS/wake backends, VAD, earcons, synthesis, announcer
  conversation/  I/O-agnostic turn loop shared by CLI, app and voice
  skills/        SKILL.md pattern library
  visuals/       delivery-agnostic rendering library
  platform/      the ONE place OS-specific primitives live
  consent/       allowlist / risk-gated consent engine
electron-app/    desktop client (React + Vite + Electron)
docs/            documentation
test/            tests
```

---

## Contributing

Issues and pull requests are welcome. Branch from `main` using a
`feat/`, `fix/`, `docs/`, `chore/`, `refactor/`, `perf/` or `test/` prefix,
keep one logical change per PR, and title it as a
[Conventional Commit](https://www.conventionalcommits.org/) — PRs are squashed,
so the title becomes the commit message.

---

## Acknowledgements

Built on [LangGraph](https://github.com/langchain-ai/langgraph),
[LangChain](https://github.com/langchain-ai/langchain) and
[deepagents](https://github.com/langchain-ai/deepagents), and speaks the
[Model Context Protocol](https://modelcontextprotocol.io/).

---

## License

Licensed under the [Apache License 2.0](LICENSE).

Copyright 2026 Abhinav. See [NOTICE](NOTICE) for attribution and third-party
dependency notes, including the LGPL position on `pynput` and `psycopg`.
