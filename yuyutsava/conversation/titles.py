"""Session title derivation — the human name a conversation list shows.

A session's title is its FIRST user message, cleaned up: the board-UI
``<selection-context>…</selection-context>`` prefix stripped (the wrapper is
composed server-side in the converse router and is invisible to the user),
whitespace collapsed, and the result truncated. Deliberately import-light so
title logic is testable without the agent stack.
"""

from __future__ import annotations

import re

TITLE_MAX = 80

# Mirrors the wrap composed in routers/converse.py (user_text handler) and the
# client-side strip in useConverse.js — keep all three in sync.
_SELECTION_BLOCK_RE = re.compile(
    r"^<selection-context>\n[\s\S]*?\n</selection-context>\n\n"
)


def strip_selection_context(text: str) -> str:
    """Drop a leading board-selection context block, if present."""
    return _SELECTION_BLOCK_RE.sub("", text or "", count=1)


def derive_session_title(text: str, max_len: int = TITLE_MAX) -> str:
    """First-message title: strip the selection block, collapse whitespace,
    truncate with an ellipsis. Returns "" when nothing usable remains."""
    t = strip_selection_context(text)
    t = " ".join(t.split()).strip()
    if not t:
        return ""
    if len(t) > max_len:
        t = t[: max_len - 1].rstrip() + "…"
    return t
