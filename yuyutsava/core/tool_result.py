"""Tool result handling: size guard + error detection.

These are runtime concerns on the streaming path, not build-time concerns.
Extracted from ``core/engine.py`` so the engine factories stay slim.

The size guard is the last-resort backstop after per-tool truncation has had
its chance (e.g. tr_read_file embeds its own SuppressedContentNotice in
``result.truncation_notice``). Anything that still exceeds
``LIMITS.max_tool_result_chars`` lands here and gets a generic notice.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import ToolMessage

from yuyutsava.core.config import LIMITS
from yuyutsava.models.tool_messages import SuppressedContentNotice, SuppressedReason


def guard_tool_result(content: str, tool_name: str) -> str:
    """Enforce the hard ceiling on tool results entering the LLM context.

    tr_read_file results are handled at the executor/agent layer — they already
    embed a SuppressedContentNotice in result.truncation_notice when has_more is
    True.  This function is the last-resort backstop for anything that still
    exceeds ``LIMITS.max_tool_result_chars`` after that processing.

    For tr_execute_in_sandbox stdout overflow a SuppressedContentNotice is
    injected into the result JSON so the LLM gets actionable recovery hints
    rather than a blind truncation.

    For any other tool a minimal notice replaces the bulk payload.
    """
    if len(content) <= LIMITS.max_tool_result_chars:
        return content

    try:
        data = json.loads(content)
        result = data.get("result") or {}

        # ── tr_execute_in_sandbox: inject notice into result, keep exit_code ──
        if tool_name in ("tr_execute_in_sandbox", "tr_grep"):
            command = ""
            if isinstance(result, dict):
                command = str(result.get("stdout", ""))[:80]  # best-effort for hint

            notice = SuppressedContentNotice.stdout_too_large(
                tool=tool_name,
                command=command,
                original_size_chars=len(content),
            )
            # Preserve exit_code and stderr (small); replace stdout with notice
            new_result: dict[str, Any] = {
                "kind": result.get("kind", "shell") if isinstance(result, dict) else "shell",
                "exit_code": result.get("exit_code") if isinstance(result, dict) else None,
                "stderr": (result.get("stderr", "") or "")[:2_000] if isinstance(result, dict) else "",
                "stdout_notice": notice.model_dump(),
            }
            data["result"] = new_result
            return json.dumps(data)

        # ── all other tools: replace entire result with a generic notice ──────
        notice = SuppressedContentNotice(
            reason=SuppressedReason.UNKNOWN,
            original_size_chars=len(content),
            tool=tool_name,
            human_message=(
                f"Tool result was {len(content):,} chars — too large to pass to the LLM. "
                f"Write large outputs to a file in the sandbox and reference the path."
            ),
            recovery=[],
        )
        data["result"] = notice.model_dump()
        return json.dumps(data)

    except Exception:
        # Non-JSON tool output (shouldn't happen for tr_* tools but be safe)
        notice = SuppressedContentNotice(
            reason=SuppressedReason.UNKNOWN,
            original_size_chars=len(content),
            tool=tool_name,
            human_message=(
                f"Tool output was {len(content):,} chars and could not be parsed. "
                f"Write large outputs to a file and reference the path."
            ),
            recovery=[],
        )
        return json.dumps({"suppressed": True, "notice": notice.model_dump()})


def is_tool_error(msg: ToolMessage, body: str) -> bool:
    """True when a ToolMessage represents a failure the user should see in red.

    Covers three cases:
      1. langchain set ``status='error'`` on the message (raised + caught).
      2. langchain's default fallback string `Error invoking tool ...`.
      3. Our tr_* JSON envelope with ``"status": "error"`` or ``"denied"``.
    """
    if getattr(msg, "status", None) == "error":
        return True
    if body.startswith("Error invoking tool"):
        return True
    stripped = body.lstrip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return False
        return isinstance(parsed, dict) and parsed.get("status") in ("error", "denied")
    return False
