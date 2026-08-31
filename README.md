# YUYUTSAVA

An AI agent that executes natural language tasks using file read/write and shell execution tools. Powered by Groq or OpenRouter LLMs via LangGraph.

---

## Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`
- Docker (only for Docker sandbox mode)

---

## Setup

### 1. Install dependencies

**With uv (recommended):**
```bash
uv sync
```

**With pip:**
```bash
pip install -e .
```

### 2. Configure `.env`

Create a `.env` file in the project root:

```env
# Choose provider: "groq" (default) or "openrouter"
LLM_PROVIDER=groq

# --- Groq ---
GROQ_API_KEY=your_groq_api_key
# Optional overrides:
# GROQ_MODEL=llama-3.3-70b-versatile
# GROQ_BASE_URL=https://api.groq.com/openai/v1

# --- OpenRouter (if LLM_PROVIDER=openrouter) ---
# OPENROUTER_API_KEY=your_openrouter_api_key
# OPENROUTER_MODEL=openai/gpt-4o-mini
# OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
# OPENROUTER_HTTP_REFERER=https://yoursite.com   # optional
# OPENROUTER_APP_TITLE=MyApp                      # optional

# --- Docker sandbox (optional) ---
# YUYUTSAVA_EXECUTION=local          # "local" or "docker"
# YUYUTSAVA_DOCKER_IMAGE=deepagent-sandbox:local
# YUYUTSAVA_DOCKER_EXPORT_DIR=/path/to/export
# YUYUTSAVA_DOCKER_NETWORK=bridge    # "bridge" or "none"
```

---

## Running the Agent

### Method 1 — CLI (installed entry point)

```bash
# Run a natural language task
yuyutsava "list all Python files in the workspace and count lines"

# With a custom workspace directory
yuyutsava --workspace ./my_project "summarize the README"

# Verbose mode (shows tool calls and results)
yuyutsava --verbose "write hello world to output.txt"
```

### Method 2 — uv run (no install needed)

```bash
uv run yuyutsava "your task here"
```

### Method 3 — Python module

```bash
python -m yuyutsava.cli.cli "your task here"
```

---

## Built-in Scenarios

Use `--scenario` to run predefined demo tasks:

```bash
# List available scenarios
yuyutsava --list-scenarios

# Run a scenario
yuyutsava --scenario explore_bash
yuyutsava --scenario read_then_summarize
yuyutsava --scenario write_artifact
yuyutsava --scenario full_loop
```

| Scenario ID          | Description                              |
|----------------------|------------------------------------------|
| `explore_bash`       | List workspace with execute tool         |
| `read_then_summarize`| Read workspace README and summarize it   |
| `write_artifact`     | Write a file describing the agent's tools|
| `full_loop`          | execute + read_file + write_file loop    |

---

## Docker Sandbox Mode

Run tools inside an isolated Docker container instead of on the host.

### Step 1 — Build the sandbox image

```bash
docker build -t deepagent-sandbox:local -f yuyutsava/docker_sandbox/Dockerfile .
```

### Step 2 — Run with Docker execution

```bash
yuyutsava --execution docker --docker-image deepagent-sandbox:local "your task"

# With an export directory for deliverables
yuyutsava --execution docker \
          --docker-image deepagent-sandbox:local \
          --docker-export-dir ./output \
          "generate a report and save it to /output/report.txt"

# Isolated network (no outbound internet in container)
yuyutsava --execution docker --docker-network none "your task"

# Pull specific paths out of the container after the run
yuyutsava --execution docker \
          --docker-export-dir ./output \
          --docker-pull-paths /workspace/result.txt,/workspace/data.csv \
          "your task"
```

---

## All CLI Flags

```
yuyutsava [task] [options]

Positional:
  task                    Natural language task to run

Options:
  --scenario, -s ID       Run a built-in scenario
  --list-scenarios        Print available scenarios and exit
  --print-tools           Print built-in tool reference (JSON) and exit
  --workspace, -w PATH    Workspace root the agent may read/write/run in (default: cwd)
  --verbose, -v           Print tool calls, results, and assistant text
  --recursion-limit N     LangGraph recursion limit (default: 200)
  --bash-timeout N        Seconds before shell command is killed (default: 120)
  --generate_agent_graph  Export agent state graph as PNG (requires network)
  --graph-dir DIR         Output directory for the graph PNG

Docker options:
  --execution {local,docker}        Where tools run (default: local)
  --docker-image IMAGE              Docker image to use
  --docker-export-dir DIR           Host directory mounted at /output in container
  --docker-network {bridge,none}    Container network mode (default: bridge)
  --docker-pull-paths PATHS         Comma-separated container paths to copy out after run
```

---

## Utility Commands

```bash
# Print all available built-in tools as JSON
yuyutsava --print-tools

# Export the agent's LangGraph state machine as a PNG
yuyutsava --generate_agent_graph
yuyutsava --generate_agent_graph --graph-dir ./diagrams
```

---

## MCP servers (`~/.yuyutsava/mcp_config.json`)

The daemon picks up MCP (Model Context Protocol) servers from
`~/.yuyutsava/mcp_config.json` at boot. The schema mirrors Claude Code's, so
existing configs can be copy-pasted:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "~/Documents"]
    },
    "spotify-local": { "url": "http://localhost:8765/mcp" }
  },
  "scopes": {
    "orchestrator":   ["spotify-local"],
    "file-organizer": ["filesystem"]
  },
  "default_scope": []
}
```

- `mcpServers`: name → either `{command, args, env}` (stdio) or `{url}` (SSE).
  `env` values support `$VAR` / `${VAR}` expansion for secrets.
- `scopes`: agent name → list of MCP server names whose tools that agent
  receives. Agents not listed get `default_scope`.
- Tools are exposed as `<server>__<tool>` so two servers can each provide a
  `read` tool without collision.
- Set `max_tools: N` on a server to cap how many tools it can expose (default
  32) — useful for misbehaving servers that flood the agent prompt.

**Hot reload:** send `SIGHUP` to the daemon (`kill -HUP <pid>`) to re-read the
config. Added / removed / changed servers are diffed; in-flight tasks finish
with the old tool list, new tasks see the new one.

Failures are non-fatal: a server that fails to start is logged and skipped;
the rest of the daemon continues normally.

### Bundled MCP server: `deepface`

YUYUTSAVA ships an in-tree DeepFace server for face detection,
identification, and enrollment. Enable it by adding to `mcp_config.json`:

```json
{
  "mcpServers": {
    "deepface": {
      "command": "uv",
      "args": ["run", "python", "-m", "yuyutsava.mcp_servers.deepface.server"]
    }
  },
  "scopes": {
    "orchestrator": ["deepface"]
  }
}
```

Install the optional dependency once: `uv sync --extra deepface` (pulls in
`deepface` + `tf-keras`, ~hundreds of MB on first run as TensorFlow caches
its weights).

Exposed tools (namespaced as `deepface__*`):

| Tool | Purpose |
|---|---|
| `detect_faces(image_path)` | Bounding boxes for every face in the image. |
| `enroll(identity, image_paths)` | Embed reference image(s) and store under `identity`. |
| `identify(image_path, threshold?)` | Closest enrolled identity (cosine ≥ threshold, default 0.4) or `null`. |
| `list_identities()` | Enrolled names + sample counts. |
| `delete_identity(identity)` | Remove every embedding for an identity. |

Embeddings live at `$YUYUTSAVA_HOME/deepface/db.sqlite` (default
`~/.yuyutsava/deepface/db.sqlite`). The default model is `Facenet512`;
embeddings stored under one model are only matched against queries from the
same model.

If the `deepface` package is missing, the server still boots and serves
`list_identities` / `delete_identity`; tool calls that need detection return
a clean error pointing at `uv sync --extra deepface`.

---

## Permission policy (`~/.yuyutsava/permissions.json`)

By default every out-of-workspace `tr_*` call shows a Tier-2 permission prompt.
The policy file lets you pre-categorise tools so trusted operations skip the
prompt and so quota-bound tools (web search) get a daily cap.

```json
{
  "tool_categories": {
    "tr_read_*":  { "policy": "auto_approve" },
    "tr_write_*": { "policy": "propose" },
    "ws_*":       { "policy": "auto_approve", "daily_cap": 50 }
  }
}
```

- Pattern keys use `fnmatch` globs; first match wins, so list specific rules
  before broad ones.
- `policy`:
  - `auto_approve` — skip the prompt for matching `tr_*` calls.
  - `propose` (default) — current behaviour; user sees a prompt.
  - `queue_for_user`, `refuse_when_no_ui` — recognised but treated as
    `propose` until the Phase-2 notification work lands.
- `daily_cap` — only meaningful for tools that pass through the cap enforcer
  (today: `ws_*`). The counter is keyed by UTC date and lives in
  `~/.yuyutsava/state.db.tool_call_counters`; the 4th call after the cap is
  hit returns a JSON refusal string instead of running.

The TaskRunner consults the policy **only on the PROMPT branch** of its rule
table — a system-critical zone is still hard-blocked regardless.

---

## Event sources (`~/.yuyutsava/events_config.json`)

Sources are registered at daemon startup and each emits onto the bus. Four
sources ship in-tree:

```json
{
  "sources": {
    "fs":        { "enabled": true, "roots": ["~/Downloads"],
                   "coalesce_window_ms": 2000 },
    "clipboard": { "enabled": true, "poll_ms": 500, "max_chars": 16384 },
    "hotkey":    { "enabled": true,
                   "bindings": { "<cmd>+<shift>+y": "ask",
                                 "<cmd>+<shift>+u": "summarize_clipboard" } },
    "appfocus":  { "enabled": true, "poll_ms": 1000,
                   "exclude_bundles": ["com.electron.yuyutsava"] }
  }
}
```

| Source | Topic | Per-event hints |
|---|---|---|
| `fs` | `fs.changed` | `path`, `ext`, `kind` (created / modified / deleted / moved) |
| `clipboard` | `clipboard.copied` | `kind` (url / path / text), `length` |
| `hotkey` | `hotkey.pressed` | `combo`, `action` (the semantic name from `bindings`) |
| `appfocus` | `app.focused` | `bundle_id`, `name` |

macOS notes:

- `hotkey` and `appfocus` require **Accessibility** permission for the parent
  terminal / Electron app (System Settings → Privacy & Security → Accessibility).
- `appfocus` is macOS-only today (uses `NSWorkspace`); on other platforms the
  source logs `unavailable` and idles. Linux parity is tracked in §9 of
  `PHASE_2_PLAN.md`.

The clipboard source dedupes via SHA-256 so a single Cmd+C only fires one
event; identical re-copies are silently dropped until the contents change.

---

## Skill-driven search scoping (`requires_tools`)

Phase-2 narrows which subagents see the web-search tools (`ws_*`). Every
`SKILL.md` may declare which tools it needs in its frontmatter:

```yaml
---
name: tavily-research
description: |
  ...
requires_tools:
  - ws_tavily_search
---
```

The orchestrator is always research-capable (gets every `ws_*` tool whose API
key is configured). Subagents only see `ws_*` tools whose name matches a
`requires_tools` glob in **at least one** of their visible skills. Today
`file-organizer` has no skill that lists `ws_*`, so it gets zero search tools
in its prompt. Add a skill with `requires_tools: [ws_tavily_search]` and that
single tool appears on its next build.

---

## License

Licensed under the [Apache License 2.0](LICENSE).

Copyright 2026 Abhinav. See [NOTICE](NOTICE) for attribution and third-party
dependency notes.
