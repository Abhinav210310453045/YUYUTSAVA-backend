"""Middleware that makes *voice* turns sound like a spoken conversation.

The daemon serves voice and text chat from the **same** agent graph; only the
per-turn ``modality`` value differs (seeded into ``configurable`` by
:func:`yuyutsava.core.streaming.astream_agent_iter`). Left alone, the agent
writes the same prose for both — so the voice channel reads out markdown,
bullet lists, code fences and long UUIDs verbatim, which sounds like a machine
reading a document rather than talking.

This middleware runs on every model call but only acts when
``configurable.modality == "voice"``: it appends a short spoken-style addendum
to the assembled system message via the public ``ModelRequest`` API (the same
post-assembly rewrite pattern :class:`FilesystemPromptOverrideMiddleware` uses).
For text turns it is a pure no-op — zero added tokens, zero behaviour change.

No agent forking, no duplicate prompt: one graph, one addendum, gated per turn.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage

# Kept as a module constant so it's importable by a fast unit check and easy to
# tune without touching the middleware logic.
VOICE_STYLE_ADDENDUM = (
    "\n\n## Speaking aloud (voice turn)\n"
    "Your reply will be spoken by a text-to-speech voice, so talk like a person "
    "on a call — do not read out a document. Follow these rules for THIS reply:\n"
    "- Keep it short: usually one to three sentences. Answer first, then stop.\n"
    "- Use plain spoken prose. No markdown, no bullet or numbered lists, no "
    "headings, no code fences, no tables, no emoji.\n"
    "- Never spell out long IDs, hashes, UUIDs, URLs or file paths character by "
    "character. Refer to them by name (e.g. \"the background task\") or read only "
    "the last few characters if the user truly needs to distinguish them.\n"
    "- Prefer natural phrasing and contractions (\"I've\", \"it's\", \"you're\").\n"
    "- If the full answer is long or has many parts, give the key point in one "
    "breath and offer to go deeper (\"want me to walk through the rest?\") instead "
    "of dumping everything at once.\n"
    "This styling applies only to what you say back to the user; it does not "
    "change which tools you call or how you do the work."
)


class VoiceStyleMiddleware(AgentMiddleware[AgentState, Any, Any]):
    """Append a spoken-style addendum to the system prompt on voice turns only."""

    def __init__(self, addendum: str = VOICE_STYLE_ADDENDUM) -> None:
        super().__init__()
        self._addendum = addendum

    @staticmethod
    def _is_voice() -> bool:
        # Read the active LangGraph RunnableConfig the same way the context
        # middleware do (see transcript_middleware / agent_context). Defensive:
        # outside a graph run, or if the shape changes, degrade to not-voice
        # (a no-op) rather than raising.
        try:
            from langgraph.config import get_config

            cfg = get_config() or {}
            conf = cfg.get("configurable", {}) or {}
            return conf.get("modality") == "voice"
        except Exception:  # noqa: BLE001 — styling never breaks a turn
            return False

    def _rewrite(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        if not self._is_voice():
            return request
        system_message = request.system_message
        if system_message is None:
            new_msg = SystemMessage(content=self._addendum.lstrip("\n"))
            return request.override(system_message=new_msg)
        blocks = list(system_message.content_blocks)
        blocks.append({"type": "text", "text": self._addendum})
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
        return await handler(self._rewrite(request))
