# Tutorials

## Deep Agents + LLM (`deep_agents_simple`)

Uses **[Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview)** (`create_deep_agent`) with an **OpenAI-compatible** chat API. Pick **Groq** or **OpenRouter** via `LLM_PROVIDER`:


| Provider       | Docs                                                                 |
| -------------- | -------------------------------------------------------------------- |
| **Groq**       | [Groq OpenAI-compatible API](https://console.groq.com/docs/overview) |
| **OpenRouter** | [OpenRouter quickstart](https://openrouter.ai/docs/quickstart)       |


Both use LangChain `ChatOpenAI` with `api_key`, `base_url`, and `model` (see `tutorials/shared/groq_chat.py`).

### Backend: local host vs Docker sandbox

**Local (default)** — `[LocalShellBackend](https://reference.langchain.com/python/deepagents/)` subclasses `FilesystemBackend` and implements `SandboxBackendProtocol`:

- File tools: `read_file`, `write_file`, `edit_file`, `ls`, `glob`, `grep` with **virtual** paths under `-w` (e.g. `/tutorials/foo.txt` → `{workspace}/tutorials/foo.txt` with `virtual_mode=True`).
- `execute` runs a shell **on your host** (not isolated). Timeout = `--bash-timeout`.

**Docker** — `tutorials/shared/docker_sandbox_backend.py` provides `DockerSandboxBackend`, a `[BaseSandbox](https://reference.langchain.com/python/deepagents/)` subclass: file tools and `execute` run **inside a container** via `docker exec`. Requires Docker CLI and a suitable image (see below).

- Host workspace (`-w`) is mounted at `/workspace` in the container; virtual paths `/...` map there.
- Optional `--docker-export-dir` mounts an extra host directory at `/output` for deliverables (or use `--docker-pull-paths` after the run to copy files out via `docker cp`).

Build the default image from the repo root:

```bash
docker build -t deepagent-sandbox:local -f tutorials/docker_sandbox/Dockerfile .
```

Then run, for example:

```bash
uv run goog --execution docker --docker-image deepagent-sandbox:local -w . --scenario read_then_summarize
```

Optional flags: `--docker-network none`, `--docker-export-dir ./exports`, `--docker-pull-paths "/tmp/out.txt,/artifacts/log.txt"` (writes under `<export-dir>/_pulled` if export dir is set, else `<workspace>/_docker_pull`).

Environment (defaults for CLI): `GOOG_EXECUTION` (`local` | `docker`), `GOOG_DOCKER_IMAGE`, `GOOG_DOCKER_EXPORT_DIR`, `GOOG_DOCKER_NETWORK`.

There is **no** separate `tutorials/shared/tools_three.py` layer — the library owns file + execute behavior.

### Shared code

- `tutorials/shared/config.py` — `GroqSettings`, `OpenRouterSettings`, `tutorial_llm_settings_from_env()` (reads `LLM_PROVIDER`)
- `tutorials/shared/groq_chat.py` — `ChatOpenAI` for Groq or OpenRouter (optional OpenRouter attribution headers)
- `tutorials/shared/deep_tutorial.py` — `build_tutorial_deep_agent` (returns `TutorialAgentBundle`: graph + optional Docker backend) + `invoke_tutorial_agent`
- `tutorials/shared/docker_sandbox_backend.py` — `DockerSandboxBackend` + `pull_virtual_paths_to_host`
- `tutorials/docker_sandbox/Dockerfile` — image for `--execution docker`

### Environment

Set `**LLM_PROVIDER`** to `groq` (default) or `openrouter`, then the matching variables.

**Groq**

- `GROQ_API_KEY` (required when `LLM_PROVIDER=groq`)
- `GROQ_BASE_URL` (optional; default Groq OpenAI base URL)
- `GROQ_MODEL` (optional)

**OpenRouter**

- `OPENROUTER_API_KEY` (required when `LLM_PROVIDER=openrouter`)
- `OPENROUTER_BASE_URL` (optional; default `https://openrouter.ai/api/v1`)
- `OPENROUTER_MODEL` (optional; default `openai/gpt-4o-mini` in code — override with any [OpenRouter model id](https://openrouter.ai/models))
- `OPENROUTER_HTTP_REFERER`, `OPENROUTER_APP_TITLE` (optional; [app attribution](https://openrouter.ai/docs/quickstart))

Load vars from `.env` in the project root (the `goog` CLI calls `load_dotenv()`).

### Run

```bash
uv sync
# Groq (default)
export GROQ_API_KEY=...
uv run goog --print-tools
uv run goog -w . -v --scenario read_then_summarize

# OpenRouter
export LLM_PROVIDER=openrouter
export OPENROUTER_API_KEY=...
# optional: export OPENROUTER_MODEL=provider/model-id
uv run goog -w . -v --scenario read_then_summarize

# to run cusotm task
uv run goog task "custom message that you want to type"
```

### Security

`LocalShellBackend` is for **trusted local** use only (full host shell + disk). See warnings in the [deepagents backend docs](https://reference.langchain.com/python/deepagents/).

Docker **reduces** host exposure (no host `execute`), but containers share the host kernel; use a minimal image, avoid mounting secrets into the container, and consider `--docker-network none` when the agent should not reach the network. Do not commit API keys; keep them in `.env` (untracked) or your shell environment.