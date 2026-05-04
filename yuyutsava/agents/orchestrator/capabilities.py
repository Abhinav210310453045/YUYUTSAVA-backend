"""
Auto-generate the orchestrator's "available subagents" block from registered
``BaseSubAgent`` instances.

This block is templated into the orchestrator's system prompt. Adding a new
subagent costs zero tokens of hand-written prompt maintenance: register it
with the daemon and the orchestrator sees it next time it boots.
"""

from __future__ import annotations

from yuyutsava.agents.base_sub_agent import BaseSubAgent


def render_capabilities_block(subagents: list[BaseSubAgent]) -> str:
    """Return one line per subagent: ``- name — description``."""
    if not subagents:
        return "  (no subagents registered)"
    lines = []
    for sa in subagents:
        desc = sa.description.strip().replace("\n", " ")
        lines.append(f"  - {sa.name} — {desc}")
    return "\n".join(lines)
