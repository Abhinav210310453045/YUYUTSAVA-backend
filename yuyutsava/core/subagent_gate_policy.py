"""Refuse delegation to subagents the user has switched off.

Phase 4 step 4.6, tenth migration (was ``SubagentGateMiddleware``), and the only
**hybrid** — it uses both hook families, which is why it went last:

* :meth:`revise_model_call` tells the model which subagents are off, so it
  routes around them instead of trying and being refused;
* :meth:`before_tool` refuses the delegation anyway, because the prompt is
  advice and the tool gate is the actual enforcement.

Both are needed. Prompt-only leaves a disabled subagent one hallucination away
from running; gate-only turns every attempt into a refused tool call the model
has to recover from mid-turn.

## The refresh

``revise_model_call`` is the one async hook on the hot path, so the settings
snapshot is refreshed there. A toggle written by *another process* — ``/subagents
off`` typed into a CLI REPL while the daemon runs — reaches this agent that way.
It is TTL-guarded inside ``RuntimeSettings``, so it is a no-op on almost every
call, and the tool gate below then reads a warm snapshot rather than doing I/O
of its own on the tool path.
"""

from __future__ import annotations

import logging
from typing import Any

from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import Denied, ModelCall, ToolCall, ToolDecision

logger = logging.getLogger("yuyutsava.core.subagent_gate_policy")

# The two ways a master hands work to a subagent.
_DELEGATION_TOOLS = frozenset({"task", "start_async_task"})

# Background variants are registered as "<name>-bg"; the toggle is on the base.
_BG_SUFFIX = "-bg"


def _base_name(subagent_type: str) -> str:
    if subagent_type.endswith(_BG_SUFFIX):
        return subagent_type[: -len(_BG_SUFFIX)]
    return subagent_type


class SubagentGatePolicy(Policy):
    """Tell the model which subagents are off, and refuse them if it tries anyway.

    Parameters
    ----------
    settings:
        A :class:`~yuyutsava.prefs.runtime.RuntimeSettings`. Read through its
        synchronous snapshot accessor, so the tool gate costs nothing per call.
        ``None`` disables the gate entirely (standalone CLI with no prefs wired).
    """

    name = "SubagentGatePolicy"

    def __init__(self, settings: Any | None) -> None:
        super().__init__()
        self._settings = settings

    # ------------------------------------------------------------------ #
    # Shared                                                              #
    # ------------------------------------------------------------------ #

    def _disabled(self) -> frozenset[str]:
        if self._settings is None:
            return frozenset()
        try:
            return self._settings.subagents().disabled
        except Exception:  # noqa: BLE001 — a toggle never breaks a turn
            logger.debug("subagent gate: settings read failed", exc_info=True)
            return frozenset()

    # ------------------------------------------------------------------ #
    # Prompt addendum                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def addendum(disabled: frozenset[str]) -> str:
        names = ", ".join(sorted(disabled))
        return (
            f"SUBAGENTS CURRENTLY TURNED OFF: {names}. The user has switched "
            "these off. Do not delegate to them (neither task nor "
            "start_async_task) — the call will be refused. Handle the work "
            "yourself or use general-purpose, and say plainly that the "
            "subagent is switched off if the user expected it."
        )

    async def revise_model_call(self, call: ModelCall) -> None:
        await self._refresh()
        disabled = self._disabled()
        if disabled:
            call.append_system_text(self.addendum(disabled))

    async def _refresh(self) -> None:
        if self._settings is None:
            return
        try:
            await self._settings.refresh()
        except Exception:  # noqa: BLE001 — a stale toggle beats a broken turn
            logger.debug("subagent gate: settings refresh failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # Tool gate                                                           #
    # ------------------------------------------------------------------ #

    async def before_tool(self, call: ToolCall) -> ToolDecision:
        blocked = self._blocked_name(call)
        if not blocked:
            return None
        logger.info("subagent gate: refused delegation to %s (off)", blocked)
        return Denied(
            f"The '{blocked}' subagent is switched off by the user. Do not retry "
            "this delegation. Do the work yourself, delegate to "
            "'general-purpose' instead, or tell the user it is turned off.",
            # Carried over from the middleware, which reported this as an error
            # and did not label the message with the tool name.
            status="error",
            named=False,
        )

    def _blocked_name(self, call: ToolCall) -> str | None:
        """The disabled subagent this call targets, or ``None`` to let it through.

        Gated on the **resolved** tool: a hallucinated or mistyped name resolves
        to nothing and must take the framework's unknown-tool path rather than be
        judged here (the middleware guarded this explicitly; its sibling did not
        — see finding BA).
        """
        if call.resolved_tool not in _DELEGATION_TOOLS:
            return None
        target = str(call.args.get("subagent_type") or "").strip()
        if not target:
            return None
        return target if _base_name(target) in self._disabled() else None


__all__ = ["SubagentGatePolicy"]
