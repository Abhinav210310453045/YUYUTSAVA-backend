"""Prompt caching for Claude where deepagents' caching does not reach it.

deepagents turns Anthropic's prompt caching on through
``AnthropicPromptCachingMiddleware``, whose gate is ``isinstance(model,
ChatAnthropic)`` (pinned by ``test/framework_contract/test_model_switch_contract.py``).
Claude on Vertex (``ChatAnthropicVertex``) and on Bedrock (``ChatBedrockConverse``)
fail that check, so a conversation that moved Claude off Anthropic's own API
would silently pay full price for every repeated prefix — the system prompt, the
tool schemas, the history — on every call.

Both classes take the same ``cache_control`` call setting ``ChatAnthropic`` does:
Vertex marks the last message's last block, Bedrock adds cache points to the
system prompt, the last message and the tools
(``test/framework_contract/test_prompt_cache_contract.py``). Anthropic's cache is
a prefix, so one mark on the last message covers everything before it. Which
models take it is :attr:`~yuyutsava.llm.handle.Capability.CACHE_CONTROL_KWARG`.

Sits after the model switch, so it asks about the model the call actually goes to.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware

from yuyutsava.llm.factory import supports
from yuyutsava.llm.handle import Capability

#: What ``AnthropicPromptCachingMiddleware`` asks for by default.
CACHE_CONTROL = {"type": "ephemeral", "ttl": "5m"}


class PromptCacheMiddleware(AgentMiddleware):  # type: ignore[misc]
    """Ask for Anthropic's prompt caching on calls to Claude outside ``ChatAnthropic``."""

    name = "PromptCache"

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        if "cache_control" not in request.model_settings and supports(
            request.model, Capability.CACHE_CONTROL_KWARG
        ):
            request = request.override(
                model_settings={**request.model_settings, "cache_control": CACHE_CONTROL}
            )
        return await handler(request)


__all__ = ["CACHE_CONTROL", "PromptCacheMiddleware"]
