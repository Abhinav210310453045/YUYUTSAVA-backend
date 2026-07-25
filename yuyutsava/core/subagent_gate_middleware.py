"""Runtime on/off switches for the *dedicated* subagents.

The user can turn an individual domain subagent (``face-watcher``,
``file-organizer``, …) off from the Settings UI or a CLI slash command. This is
deliberately NOT a switch on delegation itself: ``general-purpose`` and the
``task`` / ``start_async_task`` machinery always stay — see
:data:`yuyutsava.prefs.runtime.UNDISABLEABLE`.

Why a middleware and not just a smaller roster at build time
------------------------------------------------------------
The orchestrator builds a fresh graph per task, so filtering its roster at build
time is free and complete. The chat/voice master does not: ``ConversationManager``
caches one shared bundle for every conversation, and rebuilding it on a toggle
would be both expensive and racy with live turns. So the roster stays as-built
and this middleware enforces the toggle per model call / per tool call:

1. ``wrap_model_call`` appends one line to the system prompt naming the
   currently-off subagents — the same post-assembly rewrite pattern as
   :class:`~yuyutsava.core.voice_style_middleware.VoiceStyleMiddleware`. Without
   it the model still *sees* the agent in the baked AVAILABLE SUBAGENTS block and
   retries the refused call in a loop.
2. ``wrap_tool_call`` refuses a ``task`` / ``start_async_task`` whose
   ``subagent_type`` is off, returning a ``ToolMessage(status="error")`` — the
   same shape as
   :class:`~yuyutsava.async_subagents.cap_middleware.BackgroundTaskCapMiddleware`,
   including its ``request.tool is not None`` guard for hallucinated names.

The addendum is emitted **only when something is actually disabled**, so the
common case leaves the prompt (and therefore the provider's cache prefix) byte
for byte unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage, ToolMessage

logger = logging.getLogger("yuyutsava.core.subagent_gate_middleware")

# Both delegation tools carry the target in a ``subagent_type`` argument.
_DELEGATION_TOOLS = frozenset({"task", "start_async_task"})

# Background peers are registered as "<name>-bg" (see
# BaseSubAgent.async_subagent_name), and follow their parent's switch.
_BG_SUFFIX = "-bg"


def _base_name(subagent_type: str) -> str:
    """Map ``face-watcher-bg`` → ``face-watcher``; leave other names alone."""
    if subagent_type.endswith(_BG_SUFFIX):
        return subagent_type[: -len(_BG_SUFFIX)]
    return subagent_type


class SubagentGateMiddleware(AgentMiddleware[AgentState, Any, Any]):
    """Refuse delegation to subagents the user has switched off.

    Parameters
    ----------
    settings:
        A :class:`~yuyutsava.prefs.runtime.RuntimeSettings`. Read through its
        synchronous snapshot accessor, so this costs nothing per call. ``None``
        disables the gate entirely (standalone CLI with no prefs wired).
    """

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
    def _addendum(disabled: frozenset[str]) -> str:
        names = ", ".join(sorted(disabled))
        return (
            f"SUBAGENTS CURRENTLY TURNED OFF: {names}. The user has switched "
            "these off. Do not delegate to them (neither task nor "
            "start_async_task) — the call will be refused. Handle the work "
            "yourself or use general-purpose, and say plainly that the "
            "subagent is switched off if the user expected it."
        )

    def _rewrite(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        disabled = self._disabled()
        if not disabled:
            return request
        addendum = self._addendum(disabled)
        system_message = request.system_message
        if system_message is None:
            return request.override(system_message=SystemMessage(content=addendum))
        blocks = list(system_message.content_blocks)
        blocks.append({"type": "text", "text": addendum})
        return request.override(system_message=SystemMessage(content_blocks=blocks))

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(self._rewrite(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        # The one async hook on the hot path — refresh the snapshot here so a
        # toggle written by ANOTHER process (a `/subagents off` typed into a CLI
        # REPL) reaches this daemon's agents. TTL-guarded, so it is a no-op on
        # almost every call; the tool gate below then reads a warm snapshot.
        await self._refresh()
        return await handler(self._rewrite(request))

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

    def _blocked_name(self, request: Any) -> str | None:
        """The disabled subagent this call targets, or None to let it through."""
        tool = getattr(request, "tool", None)
        # tool is None for a hallucinated/mistyped name — let the normal
        # unknown-tool path run instead of crashing on `.name`.
        if tool is None or tool.name not in _DELEGATION_TOOLS:
            return None
        args = (getattr(request, "tool_call", None) or {}).get("args") or {}
        target = str(args.get("subagent_type") or "").strip()
        if not target:
            return None
        return target if _base_name(target) in self._disabled() else None

    def _refusal(self, request: Any, name: str) -> ToolMessage:
        tool_call_id = (getattr(request, "tool_call", None) or {}).get("id", "")
        msg = (
            f"The '{name}' subagent is switched off by the user. Do not retry "
            "this delegation. Do the work yourself, delegate to "
            "'general-purpose' instead, or tell the user it is turned off."
        )
        return ToolMessage(content=msg, tool_call_id=tool_call_id, status="error")

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        blocked = self._blocked_name(request)
        if blocked:
            logger.info("subagent gate: refused delegation to %s (off)", blocked)
            return self._refusal(request, blocked)
        return handler(request)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        blocked = self._blocked_name(request)
        if blocked:
            logger.info("subagent gate: refused delegation to %s (off)", blocked)
            return self._refusal(request, blocked)
        return await handler(request)
