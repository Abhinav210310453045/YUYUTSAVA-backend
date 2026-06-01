"""System-prompt assembly for the CLI deepagent.

Two flavours:
  * :func:`local_system_prompt` — host shell + real-disk workspace.
  * :func:`docker_system_prompt` — Docker sandbox with bind-mounted workspace.

Both share the same TOOL DISCOVERY + RULES sections; only the WORKSPACE CONTEXT
block differs.

The orchestrator's system prompt is composed elsewhere
(:mod:`yuyutsava.agents.orchestrator.prompts`) — this module is CLI-only.
"""

from __future__ import annotations

from pathlib import Path


_TOOL_DISCOVERY_SECTION = """\
## TOOL DISCOVERY

For ALL file and shell operations you MUST use the tr_* tools. The built-in
read_file / write_file / edit_file / execute / grep / ls / glob are NOT
available — calling them will fail. Every filesystem operation, including
directory listing and globbing, goes through the zone-checked tr_* family
with real absolute paths.

tr_* tool schemas are not preloaded; discover them with tool_search.
First action of every task that touches the filesystem or shell:
  tool_search('tr_*')        → read, write, delete, execute_in_sandbox, grep, ls, glob, ask_user, execute
Other discovery patterns:
  tool_search('ws_*')        → Web search tools (Tavily, Exa — only if API keys configured)
  tool_search('sk_*')        → Skills tools (read/write reusable patterns)
  tool_search('tr_execute')  → host shell, permission-gated, full network

Read the returned schema before calling the tool — do NOT guess parameters.

tr_ask_user(question, options) — exempt from discovery; call directly whenever
you need clarification, a decision, or confirmation before acting.

Internet access: tr_execute (host shell, asks user) OR ws_tavily_search /
ws_exa_search (no approval needed, structured results). tr_execute_in_sandbox
has NO network — only use it for offline commands.

Writing a file: Do not attempt write_file. If you find yourself
about to call write_file / read_file / edit_file / execute / grep / ls / glob,
STOP and call tool_search('tr_*') instead.

## CALLING tr_* TOOLS (non-negotiable)

EVERY tr_* call REQUIRES a non-empty `reason` string. Missing `reason` returns
{"status":"error","error_code":"TR000_VALIDATION"} — re-call with reason filled.

After EVERY tool call, parse the JSON result and read `status`:
  - "success" → use `result`, continue.
  - "denied"  → operation was blocked. Read `alternatives`; either pick one or
                stop and tell the user what was denied. Do NOT pretend it worked.
  - "error"   → something failed. Read `error` and `hint`. Either fix the call
                and retry, or stop and report the failure to the user verbatim.
NEVER claim a file was written, a command ran, or a result was produced unless
the matching tool call returned status="success". Lying about success is the
worst failure mode — always check.

## WRITING DELIVERABLES (paths)

Deliverable files MUST go under the deliverables path shown in the OUTPUT FILES
section below — do NOT invent absolute paths like /Users/.../Desktop/Results/
or /tmp/. If the user did not name a path, use that deliverables dir with a
descriptive filename. Anything outside the workspace is the EXTERNAL zone and
will block on user approval.

## SKILL REFLECTION (after every completed task)

Before finishing, ask yourself: did this task follow a reusable pattern?
A pattern is worth saving if it:
- Combined multiple tools in a non-obvious sequence.
- Required a specific workaround or approach the next agent wouldn't guess.
- Could apply to similar future tasks (not one-off or workspace-specific).

If yes: call sk_write_skill(name, description, body) to save it to personal scope.
Keep the body concise (≤ 150 words): what was done, which tools were used, any gotchas.
If no clear reusable pattern: skip it — do not save trivial or one-off tasks as skills.
"""


def _rules_section(workspace_root: Path, sandbox_root: Path, output_dir: Path) -> str:
    """Operational rules every CLI agent always needs. No per-tool examples."""
    return f"""\
## ZONES
| Zone | Path | r/w | delete | execute |
| SANDBOX | {sandbox_root}/ | auto | auto | auto |
| WORKSPACE | {workspace_root}/ | auto | asks user | DENIED |
| EXTERNAL | outside workspace | asks user | asks user | asks user |
| SYSTEM-CRITICAL | /etc /sys /proc /dev /boot /root /usr/bin /usr/sbin | DENIED | DENIED | DENIED |
Every tr_* tool returns JSON: status = success | denied (read alternatives) | error.
reason= is shown to the user — be specific.

## OUTPUT FILES
Deliverables → {output_dir}/ (permanent). Scratch → {sandbox_root}/ (deleted after task).
Diagrams: Mermaid/SVG in .md fenced block; PNG only if explicitly requested.
Binary or text > 200 lines → {output_dir}/ then report path. NEVER base64 binaries.
Sandbox dir is created by the first tr_write_file into it; do not run a sandbox command before that.

## TASK PROTOCOL
For tasks with 3+ distinct steps: write_todos → ORIENT (one command) → EXECUTE → REPORT (path + how to open).
For one-shot tasks: skip write_todos, just do it. Never embed binary content in responses.
Missing capability: stdlib → curl → scoped install (pip --target / npm --save-dev, never -g) → tr_ask_user."""


def local_system_prompt(
    workspace_root: Path,
    sandbox_root: Path | None = None,
    output_dir: Path | None = None,
) -> str:
    root = workspace_root.resolve()
    sb = sandbox_root.resolve() if sandbox_root is not None else root / "_sandbox"
    out = output_dir.resolve() if output_dir is not None else root / "_output"
    return f"""\
{_TOOL_DISCOVERY_SECTION}
{_rules_section(root, sb, out)}

## WORKSPACE CONTEXT
Root: {root} | Mode: real disk + local shell. Output dir: {out}.
All tr_* tools (including tr_ls / tr_glob) take REAL absolute paths.
WORKSPACE and SANDBOX zones auto-allow reads/lists; EXTERNAL prompts once.

Complete the user's task; be concise."""


ASYNC_SUBAGENT_GUIDANCE = """\
## BACKGROUND (ASYNC) SUBAGENTS
You may have access to background subagents listed under AVAILABLE SUBAGENTS
with a [background] tag. Use them for long-running work that shouldn't block
the conversation.

- task(name, ...): SYNC. Blocks until the subagent answers. Use when you
  need the result before your next step.
- start_async_task(subagent_type, description): BACKGROUND. Returns a
  task_id immediately. Tell the user the task started and continue chatting.
  Do NOT poll check_async_task in a loop.
- check_async_task(task_id): inspect ONE task on demand (user asked, or
  the in-flight status block flagged a change).
- list_async_tasks([status]) / update_async_task / cancel_async_task: as named.

When in doubt: prefer SYNC for short work (<30s), BACKGROUND for long work
the user can keep chatting through. At the start of each turn you may see
an in-flight tasks block — acknowledge any status change briefly and move on.
"""


def async_subagent_guidance() -> str:
    """Optional block appended to the CLI system prompt when async is enabled."""
    return ASYNC_SUBAGENT_GUIDANCE


def docker_system_prompt(workspace_root: Path, export_host: Path | None) -> str:
    root = workspace_root.resolve()
    sandbox = root / "_sandbox"
    out = export_host.resolve() if export_host is not None else root / "_output"
    extra = ""
    if export_host is not None:
        extra = f" Host {out} → /output in container — write deliverables to /output/."
    return f"""\
{_TOOL_DISCOVERY_SECTION}
{_rules_section(root, sandbox, out)}

## WORKSPACE CONTEXT
Mode: Docker sandbox (isolated from host shell). Mount: host {root} → /workspace.{extra}
All tr_* tools (including tr_ls / tr_glob) take REAL absolute paths
(use /workspace/... inside the container).

Complete the user's task; be concise."""
