"""The fallback safety layer for raw ``execute`` calls, as a plain policy.

Phase 4 step 4.4, first migration. ADR-004's risk table names this one to go
first: *"migrate the most demanding policy first — ``PermissionMiddleware``,
which already carries ``# type: ignore[misc]`` because the base contract does not
fit it. If the adapter handles that one, it handles the rest."*

It is the demanding case for three reasons, and each is a thing the adapter had
to be able to express:

1. it **refuses** a call outright (system-critical paths — no prompt, ever);
2. it **asks the user** and refuses on anything but approval, which is why
   ``interrupt()`` was being called from inside domain code (`F-T06`);
3. it does both **conditionally**, in a fixed order, over one tool name.

What changed and what did not: the two checks, their order, every reason string
and every ``[BLOCKED]`` message are carried over exactly — pinned by
``test/policy/test_permission_parity.py``, which runs this and the middleware it
replaced over the same command matrix and compares the results. What changed is
that the question now goes through :class:`~yuyutsava.ports.ask.AskUser`, so the
decision can be tested by scripting an answer instead of running a graph.

Checks run in this order: scope check first (stronger), then pattern check.
The first match that needs user input asks.
"""

from __future__ import annotations

from pathlib import Path

from yuyutsava.core.permission_middleware import classify_command, scope_check
from yuyutsava.models.interrupts import PermissionRequestInterrupt
from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import Denied, ToolCall, ToolDecision


class PermissionPolicy(Policy):
    """Gate raw ``execute`` calls on path scope and dangerous command shapes.

    Acts as the fallback safety layer for when the model calls ``execute``
    directly instead of routing through the TaskRunnerAgent's ``tr_*`` tools.
    """

    name = "PermissionPolicy"

    def __init__(self, workspace_root: Path | None = None) -> None:
        super().__init__()
        self.workspace_root = workspace_root.resolve() if workspace_root else None

    async def before_tool(self, call: ToolCall) -> ToolDecision:
        if call.name != "execute":
            return None

        command = call.args.get("command", "")
        if not isinstance(command, str):
            command = ""

        # ── Check 1: path scope (hard rules) ─────────────────────────────
        if self.workspace_root is not None:
            violation = scope_check(command, self.workspace_root)
            if violation is not None:
                scope_reason, hard_block = violation

                if hard_block:
                    # System-critical path: refuse immediately, no user prompt.
                    return Denied(
                        f"[BLOCKED] Access denied — system-critical path.\n"
                        f"Command : {command}\n"
                        f"Reason  : {scope_reason}"
                    )

                # Out-of-workspace or protected dir: ask the user.
                if not await self._approved(call, command, scope_reason):
                    return Denied(
                        f"[BLOCKED] User denied permission.\n"
                        f"Command : {command}\n"
                        f"Reason  : {scope_reason}"
                    )

        # ── Check 2: dangerous-command patterns (regex) ──────────────────
        pattern_reason = classify_command(command)
        if pattern_reason:
            if not await self._approved(call, command, pattern_reason):
                return Denied(
                    f"[BLOCKED] User denied permission to run this command.\n"
                    f"Command : {command}\n"
                    f"Reason  : {pattern_reason}"
                )

        return None

    async def _approved(self, call: ToolCall, command: str, reason: str) -> bool:
        """Put the question to the user; anything but ``approve`` is a refusal.

        With no way to ask — ``call.ask is None``, meaning nobody is listening —
        this refuses rather than proceeding. Silence is not consent, and for the
        one policy whose entire job is stopping destructive commands, defaulting
        the other way would be the worst possible reading. In production the
        adapter always supplies an implementation, so this is a test-shape guard,
        not a live path.
        """
        if call.ask is None:
            return False
        decision = await call.ask.ask(
            PermissionRequestInterrupt(command=command, reason=reason).to_interrupt_dict()
        )
        return decision == "approve"


__all__ = ["PermissionPolicy"]
