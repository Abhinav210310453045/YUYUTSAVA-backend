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

