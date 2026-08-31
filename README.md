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
> [docs/architecture/review/](docs/architecture/review/) for a candid
> assessment of the codebase's current structural debt.

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

cp .env.example .env      # then set ONE provider key, e.g. GROQ_API_KEY
```

Run a task:

```bash
uv run yuyutsava "list every Python file in this repo and count the lines"
```

`.env.example` is the configuration reference — 16 numbered sections, each
stating its own defaults. **Everything in it is optional except one API key.**

### Choosing a model provider

Set `LLM_PROVIDER` to any of:

| Kind | Values |
|---|---|
| OpenAI-compatible (no extra install) | `groq` · `openrouter` · `ollama` · `openai` · `openai_compatible` |
| Native SDK (`uv sync --extra <name>`) | `anthropic` · `google` (alias `gemini`) · `vertex` · `bedrock` (alias `aws`) · `azure` · `mistral` · `cohere` |

`openai_compatible` plus a base URL covers xAI, DeepSeek, Together, Fireworks,
Perplexity, Cerebras, DeepInfra and anything else speaking that API. `ollama`
runs fully local with no key.

Any role can override the main provider, so cheap work can go to a cheap model:

```bash
LLM_PROVIDER=anthropic
TRIAGE_LLM_PROVIDER=ollama
TRIAGE_OLLAMA_MODEL=llama3.2:3b
```

Roles: `triage` · `orchestrator` · `subagent` · `compaction` · `tier_light` ·
`cli` · `embed`.

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
          --docker-export-dir ./output \
          "generate a report and save it to /output/report.txt"
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
> mode and shuts off the web API the app connects to.

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
`SIGHUP`. Ships an in-tree DeepFace server as a worked example.

**Visuals** — charts, styled tables, syntax-highlighted code, math and diagrams
rendered to images the agent can hand back.

**Storage** — SQLite by default, PostgreSQL when you want durability and
semantic search, behind one dialect layer.

---

## Configuration

Three optional JSON files in `~/.yuyutsava/` (override with `YUYUTSAVA_HOME`):

| File | Controls |
|---|---|
| `mcp_config.json` | Which MCP servers start; which agents see their tools |
| `permissions.json` | Which tool calls skip the prompt; daily caps |
| `events_config.json` | Which event sources run, and their tuning |

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
| [Architecture review](docs/architecture/review/) | SOLID/DRY/KISS findings, ADRs, remediation plan |

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
  mcp_servers/   in-tree MCP servers (deepface)
  todoboard/     planning surface, cards, artifact blocks
  artifacts/     non-card artifact store for chat and voice
  audio_io/      VAD, earcons, synthesis, announcer
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
