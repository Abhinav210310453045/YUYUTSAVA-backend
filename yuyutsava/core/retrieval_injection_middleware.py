"""Middleware that injects relevant memory + skills into the prompt per turn.

The CLI deepagent is a single persistent graph (unlike the daemon orchestrator,
which rebuilds its prompt per task and injects there). To give the CLI the same
"start each turn already knowing the relevant history and skills" behavior, this
middleware runs *once per user turn* — when the latest message is the human's —
searches the injectors with that task text, and appends their blocks to the
system message.

Appending (rather than prepending) keeps the stable system-prompt prefix cached:
only the trailing, task-varying block is recomputed each turn. Injection is
skipped on the intermediate tool-loop calls within a turn (latest message is a
tool/AI message), so we don't re-embed on every model call.

Never raises — each injector already swallows its own errors; this middleware
additionally guards the whole step so a retrieval hiccup can't break a turn.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import HumanMessage, SystemMessage


class _Injector(Protocol):
    async def build_block(self, task_text: str) -> str: ...


def _content_text(msg: Any) -> str:
    """Flatten a message's content to plain text (str or block list)."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return " ".join(parts)
    return str(content)


class RetrievalInjectionMiddleware(AgentMiddleware[AgentState, Any, Any]):
    """Append top-k memory + skills blocks to the system message once per turn."""

    def __init__(self, injectors: list[_Injector | None]) -> None:
        super().__init__()
        self._injectors = [i for i in injectors if i is not None]

    def _latest_human_text(self, request: ModelRequest[Any]) -> str:
        msgs = request.messages or []
        if not msgs or not isinstance(msgs[-1], HumanMessage):
            return ""
        return _content_text(msgs[-1]).strip()

    async def _blocks(self, task_text: str) -> list[str]:
        out: list[str] = []
        for inj in self._injectors:
            try:
                block = await inj.build_block(task_text)
            except Exception:
                block = ""
            if block:
                out.append(block)
        return out

    def _apply(self, request: ModelRequest[Any], blocks: list[str]) -> ModelRequest[Any]:
        if not blocks:
            return request
        addition = "\n\n".join(blocks)
        sm = request.system_message
        if sm is None:
            return request.override(system_message=SystemMessage(content=addition))
        new_blocks = list(sm.content_blocks) + [{"type": "text", "text": "\n\n" + addition}]
        return request.override(system_message=SystemMessage(content_blocks=new_blocks))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        if self._injectors:
            task_text = self._latest_human_text(request)
            if task_text:
                request = self._apply(request, await self._blocks(task_text))
        return await handler(request)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        # Sync path can't await the async injectors; skip injection rather than
        # block the loop. The CLI/daemon run async, so awrap_model_call is used.
        return handler(request)
