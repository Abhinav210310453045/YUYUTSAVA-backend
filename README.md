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

## License

This project is licensed under the [MIT License](LICENSE).

Copyright (c) 2026 Abhinav
