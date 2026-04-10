"""
Built-in prompts for YUYUTSAVA (filesystem + ``execute``).

Paths assume ``-w`` is the repo root and ``LocalShellBackend`` uses virtual paths
(e.g. ``/yuyutsava/workspace/README.txt``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    prompt: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="explore_bash",
        title="List the workspace with execute",
        prompt=(
            "Using the execute tool only (not cat/ls via subprocess hacks if avoidable), run a short "
            "shell command to list non-hidden entries in the workspace root, then summarize what you see."
        ),
    ),
    Scenario(
        id="read_then_summarize",
        title="read_file then summarize",
        prompt=(
            "Use read_file on /yuyutsava/workspace/README.txt "
            "(virtual path under the agent workspace). Summarize it in two short sentences."
        ),
    ),
    Scenario(
        id="write_artifact",
        title="write_file artifact",
        prompt=(
            "Use write_file to create /yuyutsava/workspace/from_agent.txt with exactly three lines: "
            "(1) mention read_file, write_file, and execute, "
            "(2) one sentence on read_file, (3) one sentence on execute. Confirm the path."
        ),
    ),
    Scenario(
        id="full_loop",
        title="execute + read_file + write_file",
        prompt=(
            "1) Use execute to echo 'step1'. "
            "2) read_file /yuyutsava/workspace/README.txt "
            "3) write_file /yuyutsava/workspace/loop_result.txt with the first line of that README "
            "plus a line 'done'. One-sentence summary for the user."
        ),
    ),
)

_BY_ID = {s.id: s for s in SCENARIOS}


def get_scenario(scenario_id: str) -> Scenario:
    s = _BY_ID.get(scenario_id)
    if s is None:
        known = ", ".join(sorted(_BY_ID))
        raise ValueError(f"Unknown scenario {scenario_id!r}. Choose one of: {known}")
    return s


def format_scenario_list() -> str:
    lines = ["Scenarios (use --scenario <id>):", ""]
    for s in SCENARIOS:
        lines.append(f"  {s.id}")
        lines.append(f"      {s.title}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
