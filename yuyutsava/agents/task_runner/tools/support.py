"""Shared plumbing for the TaskRunner tool families: the context every tool is
built from, path resolution, and the validation-error shape."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
