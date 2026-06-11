"""
Middleware that removes (or rewords) the deepagents filesystem system-prompt block.

`deepagents` auto-adds ``FilesystemMiddleware`` (a ``_REQUIRED_MIDDLEWARE`` we cannot
exclude). On every model call it appends a "## Following Conventions / ## Filesystem
Tools / ## Large Tool Results / ## Execute Tool" block ("BLOCK C") to the system
message, advertising the built-in ``read_file / write_file / edit_file / ls / glob /
grep / execute`` tools.

That block is wrong for us: those built-ins are filtered out of the model's toolbelt by
``ToolFilterMiddleware``, and our own system prompt (``local_system_prompt``) already
tells the model to use the ``tr_*`` family instead. Left in, BLOCK C contradicts our
prompt and wastes ~700 cache-prefix tokens per turn.

We do NOT edit the library. ``FilesystemMiddleware`` runs untouched and still appends
BLOCK C; this middleware — registered *after* it, so it sees the assembled system
message — rewrites ``request.system_message`` before the model call using only the
public ``ModelRequest`` API. That keeps us safe across ``deepagents`` upgrades. This is
the same post-assembly rewrite pattern ``ToolFilterMiddleware`` uses for tools.

  replacement=None  -> drop the block entirely (default)
  replacement="..." -> replace the block's text with custom wording
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

# Import the library's own constant so our matcher tracks its wording across upgrades:
# if deepagents rewords the block, this import moves with it and the match still holds.
# If the constant is ever moved/renamed we fall back to a stable heading anchor, and
# failing that we no-op (BLOCK C reappears, never a crash).
try:
    from deepagents.middleware.filesystem import FILESYSTEM_SYSTEM_PROMPT as _FS_PROMPT
except Exception:  # pragma: no cover - import guard for future library moves
    _FS_PROMPT = None

_ANCHOR = "## Filesystem Tools"  # stable fallback marker


class FilesystemPromptOverrideMiddleware(AgentMiddleware[AgentState, Any, Any]):
    """Strip or replace the filesystem block that ``FilesystemMiddleware`` appends.

    Pass ``replacement=None`` (default) to drop the block, or a string to swap its
    text for custom wording (e.g. redirecting the model to the ``tr_*`` tools).
    """

    def __init__(self, replacement: str | None = None) -> None:
        super().__init__()
        self._replacement = replacement

    def _is_fs_block(self, text: str) -> bool:
        stripped = text.strip()
        if _FS_PROMPT and stripped.startswith(_FS_PROMPT.strip()):
            return True
        return _ANCHOR in stripped

    def _rewrite(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        system_message = request.system_message
        if system_message is None:
            return request

        new_blocks: list[Any] = []
        changed = False
        for block in system_message.content_blocks:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and self._is_fs_block(block.get("text", ""))
            ):
                changed = True
                if self._replacement is not None:
                    new_blocks.append({"type": "text", "text": self._replacement})
                # replacement is None -> drop the block entirely
                continue
            new_blocks.append(block)

        if not changed:
            return request
        return request.override(system_message=SystemMessage(content_blocks=new_blocks))

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
