"""Shared plumbing for the TaskRunner tool families.

The agent itself is cached per workspace so every family — and every subagent
bound to the same workspace — talks to one instance, and the process-wide policy
and consent registries are installed here. Those two are the `set_default_*`
singletons `R-GLB-01` retires in W5; they keep their names for now so the
bootstrap that calls them is untouched by this split.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.storage.paths import WorkspaceLayout

_log = logging.getLogger("yuyutsava.agents.task_runner.tools.support")

# One TaskRunnerAgent per workspace root. Subagents bound to the same workspace
# share it, so they also share its exec backend and its policy decisions.
_registry: dict[str, TaskRunnerAgent] = {}
_default_policy: object | None = None  # PermissionsPolicy; set by daemon at boot
_default_consent: object | None = None  # consent.ConsentRegistry; set at boot


@dataclass(frozen=True)
class ToolContext:
    """What every TaskRunner tool needs to build a request.

    All twelve tools captured exactly these three from the old factory closure,
    so they travel together rather than as three arguments repeated per family.
    """

    agent: Any
    workspace_root: Path
    agent_name: str


def resolve_path(raw: str, workspace_root: Path) -> str:
    """Normalize a tr_* path argument.

    Contract: tr_* tools take REAL absolute paths. Relative paths are
    resolved against workspace_root explicitly (not against process cwd).
    Non-existent absolute paths surface as clean FileNotFoundError downstream
    rather than being silently rewritten.
    """
    if not raw:
        return raw
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = str(workspace_root.resolve() / expanded)
    return os.path.normpath(expanded)


def validation_error_json(exc: Exception) -> str:
    """Return a structured JSON error so the LLM sees `status: error` instead of a
    langchain `Error invoking tool ...` string it tends to ignore.
    Attached as ``handle_validation_error=`` on every tr_* @tool so pydantic
    arg-validation failures (missing reason=, wrong type, etc.) become a normal
    OperationResponse-shaped result the model can react to.
    """
    msg = str(exc).replace("\n", " ")
    return json.dumps(
        {
            "status": "error",
            "error_code": "TR000_VALIDATION",
            "error": f"Tool arguments invalid: {msg}",
            "hint": (
                "All tr_* tools require a non-empty `reason` string describing why the "
                "operation is needed. Re-call the tool with every required argument."
            ),
        }
    )


def set_default_policy(policy: object | None) -> None:
    """Install a permissions policy for every TaskRunnerAgent the registry mints.

    The daemon calls this once at startup. Already-cached agents are updated
    in place so a hot reload picks up the new policy without rebuilding tools.
    """
    global _default_policy
    _default_policy = policy
    for agent in _registry.values():
        agent._policy = policy  # type: ignore[attr-defined]


def set_default_consent(consent: object | None) -> None:
    """Install the consent (allowlist) registry for every minted TaskRunnerAgent.

    Mirrors :func:`set_default_policy`: the daemon/CLI calls this once at startup
    and already-cached agents are updated in place so the allowlist is shared by
    every ``tr_*`` tool call.
    """
    global _default_consent
    _default_consent = consent
    for agent in _registry.values():
        agent._consent = consent  # type: ignore[attr-defined]


def _get_or_create_agent(workspace_root: Path, sandbox_root: Path | None = None) -> TaskRunnerAgent:
    ws = workspace_root.resolve()
    # Resolve the default BEFORE keying: a caller that omits sandbox_root and
    # one that passes the layout default must land on the SAME cached agent
    # (master, subagents and spawn.py used to mint three per workspace).
    sb = (
        sandbox_root.resolve()
        if sandbox_root is not None
        else WorkspaceLayout.for_workspace(ws).sandbox
    )
    key = f"{ws}|{sb}"
    if key not in _registry:
        _registry[key] = TaskRunnerAgent(
            ws,
            sandbox_root=sb,
            policy=_default_policy,
            consent=_default_consent,
        )
    return _registry[key]
