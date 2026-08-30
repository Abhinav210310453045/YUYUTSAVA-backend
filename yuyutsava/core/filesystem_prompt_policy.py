"""Strip the filesystem block deepagents appends to every system prompt.

Phase 4 step 4.6, eighth migration (was ``FilesystemPromptOverrideMiddleware``).

``FilesystemMiddleware`` is required middleware inside ``create_deep_agent``, and
it appends a "## Filesystem Tools" block describing ``read_file``/``write_file``/
``execute``. This system replaces those with zone-checked ``tr_*`` equivalents,
so the block contradicts our own prompt and has to go.

## This is a Phase 0 silent-failure seam

The match is against *deepagents' wording*. If an upgrade rewords the block,
moves ``FILESYSTEM_SYSTEM_PROMPT``, or changes the heading, the matcher finds
nothing — and the old behaviour was to return the request unchanged and stay
quiet, so the block would silently reappear in every prompt. That is exactly the
failure this class exists to prevent, which is why
:meth:`FilesystemPromptPolicy._warn_no_match_once` exists and why
``test/test_filesystem_prompt_override.py`` is negative-controlled.

The warning is once per instance: this runs on every model call, and a per-turn
warning is noise a user learns to ignore.
"""

from __future__ import annotations

import logging

from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import ModelCall

# deepagents' own block text, when importable — the precise match. We fall back
# to the heading anchor if the constant moves, and failing that we no-op (BLOCK C
# reappears, never a crash).
try:
    from deepagents.middleware.filesystem import FILESYSTEM_SYSTEM_PROMPT as _FS_PROMPT
except Exception:  # pragma: no cover - import guard for future library moves
    _FS_PROMPT = None

_ANCHOR = "## Filesystem Tools"  # stable fallback marker

logger = logging.getLogger("yuyutsava.core.filesystem_prompt_policy")


def is_filesystem_block(text: str) -> bool:
    """Whether *text* is deepagents' filesystem block."""
    stripped = text.strip()
    if _FS_PROMPT and stripped.startswith(_FS_PROMPT.strip()):
        return True
    return _ANCHOR in stripped


class FilesystemPromptPolicy(Policy):
    """Drop or replace the filesystem block ``FilesystemMiddleware`` appends.

    Pass ``replacement=None`` (default) to drop the block, or a string to swap
    its text for custom wording (e.g. redirecting the model to the ``tr_*``
    tools).
    """

    name = "FilesystemPromptPolicy"

    def __init__(self, replacement: str | None = None) -> None:
        super().__init__()
        self._replacement = replacement
        self._warned_no_match = False
        self._matched_at_least_once = False

    async def revise_model_call(self, call: ModelCall) -> None:
        matched = False
        for index, text in call.text_blocks():
            if is_filesystem_block(text):
                matched = True
                call.rewrite_system_block(index, self._replacement)
        if matched:
            self._matched_at_least_once = True
        else:
            self._warn_no_match_once()

    def _warn_no_match_once(self) -> None:
        """Stripping nothing is never the intended outcome — say so, once.

        ``FilesystemMiddleware`` appends the block on every model call, so a
        no-match means our matcher stopped recognising it — most likely a
        ``deepagents`` upgrade reworded the block, moved
        ``FILESYSTEM_SYSTEM_PROMPT``, or changed the ``## Filesystem Tools``
        heading.
        """
        if self._warned_no_match or self._matched_at_least_once:
            return
        self._warned_no_match = True
        logger.warning(
            "FilesystemPromptPolicy matched no filesystem block — the "
            "deepagents '## Filesystem Tools' block is NOT being stripped and is "
            "reaching the model, contradicting our tr_* prompt. Likely a deepagents "
            "upgrade changed the block's wording. Run "
            "test/test_filesystem_prompt_override.py to confirm."
        )


__all__ = ["FilesystemPromptPolicy", "is_filesystem_block"]
