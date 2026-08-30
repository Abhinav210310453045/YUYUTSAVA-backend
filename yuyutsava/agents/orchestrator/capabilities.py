"""
Auto-generate the orchestrator's "available subagents" block from registered
``BaseSubAgent`` instances.

This block is templated into the orchestrator's system prompt. Adding a new
subagent costs zero tokens of hand-written prompt maintenance: register it
with the daemon and the orchestrator sees it next time it boots.

Async (background) subagents appear with a ``[background]`` tag and an extra
invocation hint so the master knows to use ``start_async_task`` rather than
the sync ``task`` tool.
"""

from __future__ import annotations

from yuyutsava.agents.base_sub_agent import BaseSubAgent


def render_capabilities_block(
    subagents: list[BaseSubAgent],
    *,
    async_subagents: list[BaseSubAgent] | None = None,
    remote_async_subagents: list[object] | None = None,
    disabled: "frozenset[str] | set[str] | None" = None,
) -> str:
    """Return one line per subagent.

    Format:
      - ``<name>`` ``[sync]`` — ``<description>``
      - ``<name>-bg`` ``[background, local]`` — start with ``start_async_task('<name>-bg', ...)``
      - ``<name>`` ``[background, remote]`` — same, hosted off-process

    ``disabled`` names subagents the user switched off at runtime
    (``runtime.subagents``); they are omitted entirely — from the sync roster,
    their ``-bg`` peers, and the remote entries — so a master that reads this
    block never learns about a subagent it isn't allowed to call. Callers whose
    graph outlives a toggle also install
    :class:`~yuyutsava.core.subagent_gate_policy.SubagentGatePolicy`.

    Empty input → ``(no subagents registered)``.
    """
    off = frozenset(disabled or ())
    lines: list[str] = []
    for sa in subagents or []:
        if sa.name in off:
            continue
        desc = sa.description.strip().replace("\n", " ")
        lines.append(f"  - {sa.name} [sync] — {desc}")

    for sa in async_subagents or []:
        if not getattr(sa, "supports_async", False) or sa.name in off:
            continue
        async_name = sa.async_subagent_name()
        desc = sa.description.strip().replace("\n", " ")
        lines.append(
            f"  - {async_name} [background, local] — {desc} "
            f"Start via start_async_task(subagent_type='{async_name}', description=...)."
        )

    for r in remote_async_subagents or []:
        # ``RemoteAsyncSubagentSpec`` exposes name/description directly.
        name = getattr(r, "name", "?")
        if name in off:
            continue
        desc = (getattr(r, "description", "") or "").strip().replace("\n", " ")
        lines.append(
            f"  - {name} [background, remote] — {desc} "
            f"Start via start_async_task(subagent_type='{name}', description=...)."
        )

    if not lines:
        return "  (no subagents registered)"
    return "\n".join(lines)
