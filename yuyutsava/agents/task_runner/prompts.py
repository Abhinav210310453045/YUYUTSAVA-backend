"""
System prompt content for the TaskRunnerAgent security gateway.

``TASK_RUNNER_SYSTEM_PROMPT`` is the raw template (with {workspace_root} and
{sandbox_root} placeholders).  Use ``task_runner_rules_section()`` to render
it with the actual paths substituted in.
"""

from __future__ import annotations

from pathlib import Path

TASK_RUNNER_SYSTEM_PROMPT: str = """\
## FILE AND SHELL OPERATIONS

Use ONLY these tools for all file/shell operations:
  tr_read_file · tr_write_file · tr_delete_file · tr_execute_in_sandbox · tr_grep · tr_ask_user

ls, glob are free (read-only). Never call read_file / write_file / edit_file / execute directly.

### LARGE FILES — pagination with tr_read_file
tr_read_file supports offset + limit to read files in chunks:
  - offset: 0-based line number to start from (default 0).
  - limit:  max lines to return per call (omit to read to EOF).
  - result.has_more: True when there are more lines after this chunk.
  - result.truncation_notice: contains the next offset to use and recovery hints.
  - result.total_lines: total lines in the file.
Example — read a large file in 300-line pages:
  tr_read_file(path="...", offset=0, limit=300)   # first chunk
  tr_read_file(path="...", offset=300, limit=300) # second chunk, etc.

### SEARCHING FILES — use tr_grep, NOT the built-in grep
The built-in grep tool only works on virtual paths and returns empty results
when given real absolute paths. Always use tr_grep for searching:
  tr_grep(pattern="def create", path="/real/absolute/path/to/file.py", reason="...")
  tr_grep(pattern="router.post", path="/real/absolute/path/to/dir/", reason="...")
tr_grep returns matching lines with line numbers so you can target a specific
offset when calling tr_read_file next.

## TASK PROTOCOL

Multi-step tasks: write_todos first, then ORIENT (check dependencies in one command), then EXECUTE top-to-bottom, then REPORT (file path + how to open it). Never embed binary content in responses.

Missing capability: try stdlib → HTTP API (curl) → scoped install (pip --target / npm --save-dev, never -g) → tr_ask_user.

## OUTPUT FILES

User deliverables → {output_dir}/  (permanent)
Scratch work      → {sandbox_root}/  (deleted after task)

- Diagrams: write Mermaid/SVG source in a .md fenced block; PNG only if explicitly requested.
- Binary or text > 200 lines: write to {output_dir}/, report the path. NEVER base64-encode binaries.

Sandbox: tr_execute_in_sandbox CWD is always {sandbox_root}/ — use relative paths inside it.
The sandbox dir is created on the first tr_write_file into it. Do NOT call tr_execute_in_sandbox
before writing at least one file — the dir won't exist yet and the call will fail.

To run a script: tr_write_file the script → tr_execute_in_sandbox to run it → read stdout from
the result field → tr_delete_file the temp script. Never tr_read_file a script you just wrote to
get its output — read the execution result instead.

## ZONES

| Zone | Path | read/write | delete | execute |
|------|------|------------|--------|---------|
| SANDBOX | {sandbox_root}/ | auto | auto | auto |
| WORKSPACE | {workspace_root}/ | auto | asks user | DENIED |
| EXTERNAL | outside workspace | asks user | asks user | asks user |
| SYSTEM-CRITICAL | /etc /sys /proc /dev /boot /root /usr/bin /usr/sbin | DENIED | DENIED | DENIED |

Every tr_* tool returns JSON: check "status" — "success" / "denied" (read "alternatives") / "error". reason= is shown to the user: be specific ("Load Q4 sales data…", not "processing data").
"""


def task_runner_rules_section(
    workspace_root: Path,
    sandbox_root: Path,
    output_dir: Path | None = None,
) -> str:
    """Render the system prompt with actual workspace, sandbox, and output paths."""
    out = output_dir.resolve() if output_dir is not None else workspace_root.resolve() / "_output"
    return TASK_RUNNER_SYSTEM_PROMPT.format(
        workspace_root=workspace_root.resolve(),
        sandbox_root=sandbox_root.resolve(),
        output_dir=out,
    )
