# Configuration Reference

YUYUTSAVA reads three JSON config files from its state directory
(`~/.yuyutsava/` by default, override with `YUYUTSAVA_HOME`). All three are
optional — the system boots with sensible defaults if none exist.

| File | Controls |
|---|---|
| `mcp_config.json` | Which MCP servers start, and which agents see their tools |
| `permissions.json` | Which tool calls skip the permission prompt, and daily caps |
| `events_config.json` | Which event sources run and how they are tuned |

Environment variables are documented inline in
[`.env.example`](../../.env.example), which is organised into 16 numbered
sections and states its own defaults.

Everything yuyutsava writes *inside* a workspace lives in one hidden
directory, `<workspace>/.yuyutsava/` — see
[Per-workspace state](#per-workspace-state-workspaceyuyutsava). For what the
state directory itself holds, see
[What lives in `~/.yuyutsava/`](#what-lives-in-yuyutsava-global-state);
for how a long conversation stays inside the model's budget without losing
anything, see [Context management](#context-management-what-the-agent-can-still-reach).

---

## MCP servers (`mcp_config.json`, global + per workspace)

MCP (Model Context Protocol) servers are read from two files with the same
schema — it mirrors Claude Code's, so existing configs can be copy-pasted:

| File | Scope |
|---|---|
| `~/.yuyutsava/mcp_config.json` | global — every workspace |
| `<workspace>/.yuyutsava/mcp_config.json` | this workspace only; honoured when the workspace is **trusted** (below) |

Both the daemon (at boot, for its `--workspace`) and the standalone CLI
(`yuyutsava …`, for its `--workspace`) load the merged result and start the
servers; the CLI stops them again when it exits.

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "~/Documents"]
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "$GITHUB_TOKEN" }
    },
    "spotify-local": { "url": "http://localhost:8765/mcp" }
  },
  "scopes": {
    "cli":            ["filesystem", "github"],
    "orchestrator":   ["spotify-local", "github"],
    "file-organizer": ["filesystem"]
  },
  "default_scope": [],
  "trusted_workspaces": ["/Users/me/projects/app"]
}
```

- `mcpServers`: name → either `{command, args, env}` (stdio) or `{url}` (SSE).
  `$VAR` / `${VAR}` expand in `command`, every `args` item, `url` and `env`
  values; `${YUYUTSAVA_WORKSPACE}` is the workspace root the config is loaded
  for, so a workspace file can point at its own `.yuyutsava/scripts/`.
- `scopes`: agent name → list of MCP server names whose tools that agent
  receives. Agents not listed get `default_scope`. The masters are `cli`
  (chat/voice and the standalone CLI), `orchestrator` (daemon) and `tinker`;
  subagents use their names (`file-organizer`, `face-watcher`,
  `general-purpose`).
- `trusted_workspaces` (global file only): a workspace file spawns processes,
  so it is used only for workspaces listed here. Any other workspace's file is
  logged (`mcp: … ignored — add … to "trusted_workspaces"`) and skipped.
- **Merge:** a workspace server overrides a global one of the same name;
  `scopes` and `default_scope` are unioned per agent (global first).
- Tools are exposed as `<server>__<tool>` so two servers can each provide a
  `read` tool without collision.
- Set `max_tools: N` on a server to cap how many tools it can expose (default
  32) — useful for misbehaving servers that flood the agent prompt.

**Hot reload:** send `SIGHUP` to the daemon (`kill -HUP <pid>`) to re-read
both files. Added / removed / changed servers are diffed; in-flight tasks
finish with the old tool list, new tasks see the new one.

Failures are non-fatal: a server that fails to start is logged and skipped;
the rest of the daemon (or CLI) continues normally. In the interactive CLI,
server output during startup is suppressed with the rest of the plumbing
noise — failures still reach the log.

---

## Per-workspace state (`<workspace>/.yuyutsava/`)

Everything yuyutsava writes *inside* a workspace lives in one hidden
directory, created on first use. `WorkspaceLayout` in
[`yuyutsava/storage/paths.py`](../../yuyutsava/storage/paths.py) is the single
source of truth for these paths — nothing joins them by hand.

| Path | Purpose |
|---|---|
| `.gitignore` | Contains `*`: the whole directory is machine-local and never committed. Your own `.gitignore` is not touched. |
| `ChangeLog.md` | Appended after every task that changed files: `## <UTC ISO> · <agent> · <gist>` then one `- <path> — <what changed> (<level>)` line per file, level ∈ file/module/config/docs/test/deps. A gist, never the diff. |
| `Assumptions.md` | Appended when the agent settles something you left ambiguous: `## <UTC ISO> · <gist>` then `- ASSUMED: <what> — because <why>`. |
| `workspace_memory.md` | Durable facts about this workspace, one dated, tagged bullet each: `- <YYYY-MM-DD> [layout\|conventions\|gotchas\|decisions] <fact>`. |
| `mcp_config.json` | Optional workspace-level MCP servers (see above; needs `trusted_workspaces`). |
| `skills/<name>/SKILL.md` | Workspace-scope skills — highest precedence in the skill registry. |
| `scripts/` | Reusable scripts the agent wrote; run with `tr_run_python`. Kept across tasks. |
| `outputs/` | Deliverables (`YUYUTSAVA_OUTPUT_DIR` / `--output-dir` override the location). |
| `sandbox/` | Scratch — the SANDBOX zone (`YUYUTSAVA_SANDBOX_DIR` / `--sandbox-dir` override). Created on demand, wiped after each CLI task. |
| `tmp/` | deepagents scratch (`large_tool_results/`, `conversation_history/`): wiped after a CLI task, TTL-swept (24 h) by the daemon. |

The three Markdown files are append-only (`tr_write_file(append=True)`). The
agent greps them for the task's keywords (`tr_grep … <workspace>/.yuyutsava`)
before starting rather than reading them whole, and a workspace-wide
`tr_grep` / `tr_glob` skips `.yuyutsava/` unless it is the search root, so
scratch never pollutes a code search.

Running from your home directory — where `.yuyutsava` *is* the global state
dir — routes the per-workspace state to `~/.yuyutsava/workspaces/home/`.

**Docker mode** (`--execution docker`): `<workspace>/.yuyutsava` is mounted
read-write at `/yuyutsava` and the workspace read-only at `/workspace`.
`tr_execute_in_sandbox` and `tr_run_python` run inside the container (cwd
`/yuyutsava/sandbox`); every other `tr_*` tool works on the host over the
same files, so a deliverable written to `/yuyutsava/outputs` is already in
`<workspace>/.yuyutsava/outputs`. Keep any sandbox override under
`.yuyutsava`, or it will not be visible inside the container.

---

## What lives in `~/.yuyutsava/` (global state)

The per-user state directory (`YUYUTSAVA_HOME` overrides it). What is *live*
depends on the storage backend, so the same directory can hold files nothing
reads any more.

| Entry | When it is live |
|---|---|
| `mcp_config.json`, `permissions.json`, `events_config.json` | Always — the three config files above. |
| `.env` | Always. App-managed overrides the daemon loads *after* the project `.env`. |
| `skills/<name>/SKILL.md` | Always. Personal-scope skills (`sk_write_skill` writes here). |
| `agents/<agent>/memory/` | Always. Per-agent learned behaviour (`um_*`), with `MEMORY.md` as the injected index. |
| `blobs/` | Always. `artifacts/` (rich artifacts), `todoboard/`, `voice/`, `webcam/`. |
| `model_prices.json`, `.model_prices_cache.json` | Always. Price table for the cost ledger; add an entry for your model or costs record as 0. |
| `api_token` | Always. Local daemon API token. |
| `chat_history` | Always. The REPL's up-arrow history. Never read by the agent, never in a prompt; rotated at 10 MB to `chat_history.1`. |
| `state.db` | Always. On Postgres it is the spillover write buffer plus the standalone-CLI fallback for a few stores. |
| `sessions.db`, `checkpoints.db`, `interrupts.db` | **SQLite backend only.** On Postgres (`YUYUTSAVA_STORAGE_BACKEND=postgres`) sessions, checkpoints and interrupts live in Postgres and these files are stale leftovers — safe to archive. |
| `migrations.lock` | SQLite backend only. |

The chat banner prints the effective backend (`storage: postgres`), which is
resolved from `StorageSettings`, not from `YUYUTSAVA_SESSIONS_BACKEND` —
that variable only picks the checkpointer default.

---

## Context management (what the agent can still reach)

Two mechanisms keep a long conversation inside the model's input budget.
Neither discards anything: both leave an addressable way back, and every
reader pages, so no single read can be truncated into a dead end.

| Mechanism | What leaves the prompt | How the agent gets it back |
|---|---|---|
| **Tool-result offload** — results over `YUYUTSAVA_CONTEXT_OFFLOAD_THRESHOLD_CHARS` (20k), and anything from a `ws_*` search | The body; a digest with `artifact_id`, head and tail stays | `ctx_fetch_artifact(id, offset=…)` or `(id, start_line=…)`, `ctx_grep_artifact(id, pattern)`, `ctx_recall(query)` (Postgres) |
| **Compaction** — fires past `compact_fraction` × the input budget | Older turns, replaced by a structured summary | `ctx_history(after_seq=…)`, `ctx_history_grep(pattern)`, `ctx_history_message(seq)` — the verbatim messages, from the transcript store |

The input budget defaults to the provider's real window (1,000,000 for
Vertex/Gemini, 200,000 Anthropic, 128,000 Groq/OpenRouter, 8,192 Ollama);
`YUYUTSAVA_CONTEXT_MAX_INPUT_TOKENS` overrides it, per role with a prefix
(`CLI_…`, `ORCHESTRATOR_…`). Setting it low is not the safe direction — it
makes compaction fire sooner, and compaction is the only step that removes
messages from the live context.

A single `ctx_*` read returns at most 40,000 characters and ends in a
`[more: …]` line naming the next offset or line. That ceiling is what keeps
the readers exempt from offload safely: an unbounded read would cross the
100,000-character result limit and be replaced by a notice.

`/usage` in the chat REPL reports the session's calls, tokens and estimated
cost, and each turn prints its own totals. Rows land in the same `llm_usage`
table the daemon writes.

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
  source logs `unavailable` and idles. Linux parity is not implemented.

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

