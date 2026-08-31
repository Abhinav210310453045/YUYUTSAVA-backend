"""Hide tool schemas the model should not see up front.

Phase 4 step 4.6, sixth migration (was ``ToolFilterMiddleware``).

Two suppression layers:

1. Named deepagents built-ins (read_file, write_file, edit_file, execute, grep,
   ls, glob) — replaced by tr_* equivalents; sending both wastes ~700 tokens
   and risks the model choosing the unguarded versions. ls/glob are filtered
   because their virtual-path mode silently returns [] for any real path
   outside the workspace (see tr_ls / tr_glob, which are zone-checked).

2. Our custom tool prefixes (tr_*, ws_*, sk_*, fo_*, ev_*, db_*, mem_*) — these
   are injected into the graph for execution but hidden from the model's initial
   view. It sees their NAMES in the always-visible catalog (built by
   ``ToolRegistry.catalog_block`` and injected into the system prompt) and loads
   a schema on demand via ``tool_search(\'select:<name>\')`` or a keyword search.
   This is the ToolRegistry progressive-discovery pattern: names are cheap and
   always shown, full schemas are served on request to save tokens.

Tools still exist in the graph (ToolMessage handlers work); the model just never
sees their schemas until it pulls them with ``tool_search``.

The decision — :func:`should_suppress` — was already a pure function at module
scope. What the migration removes is the ``ModelRequest``/``override`` plumbing
around it, which is now the adapter\'s.
"""

from __future__ import annotations

from yuyutsava.policy.base import Policy
from yuyutsava.policy.types import ModelCall

# Exact-name suppression: deepagents built-in tools replaced by tr_* equivalents.
_SUPPRESS_NAMES: frozenset[str] = frozenset({
    "read_file", "write_file", "edit_file", "execute", "grep", "ls", "glob",
})

# Prefix suppression: all our custom-prefixed tools are hidden from the model
# upfront. Their names stay visible in the system-prompt catalog; the agent
# pulls a schema on demand via tool_search(\'select:<name>\') or a keyword search.
#
# ctx_* is deliberately NOT here: offload digests reference
# ctx_fetch_artifact / ctx_grep_artifact directly, so the model must always
# see their schemas (same always-visible treatment as tool_search itself).
_SUPPRESS_PREFIXES: tuple[str, ...] = (
    "tr_", "ws_", "sk_", "fo_", "ev_", "db_", "mem_", "todo_", "um_", "orch_",
)


def should_suppress(name: str) -> bool:
    """Whether *name*\'s schema is hidden from the model\'s initial view."""
    if name in _SUPPRESS_NAMES:
        return True
    return name.startswith(_SUPPRESS_PREFIXES)


class ToolFilterPolicy(Policy):
    """Strip redundant and lazy-discovery tool schemas before every model call."""

    name = "ToolFilterPolicy"

    async def revise_model_call(self, call: ModelCall) -> None:
        call.suppress_tools(n for n in call.tool_names if should_suppress(n))


__all__ = ["ToolFilterPolicy", "should_suppress"]
