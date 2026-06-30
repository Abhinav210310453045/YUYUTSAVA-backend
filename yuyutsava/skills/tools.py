"""
LangChain tools that expose the SkillRegistry to agents.

read_skill  — load a full SKILL.md body on demand (agent-invoked)
write_skill — save a compact pattern to personal scope (orchestrator-only)

When a :class:`~yuyutsava.skills.store.SkillStore` is supplied, ``sk_write_skill``
dual-writes: the on-disk ``SKILL.md`` stays the source of truth, and the store is
updated as the semantic index so the skill is retrievable in later sessions. A
store failure never loses the on-disk skill.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool, tool

from yuyutsava.skills.registry import SkillRegistry

if TYPE_CHECKING:
    # Import only for typing — skills.store pulls in the memory.embedder chain,
    # and skills.tools is imported during core init (base_sub_agent), so a
    # runtime import here would be circular.
    from yuyutsava.skills.store import SkillStore

logger = logging.getLogger("yuyutsava.skills.tools")


def make_skill_tools(
    registry: SkillRegistry, store: SkillStore | None = None
) -> list[BaseTool]:
    """Return skill tools bound to registry (+ optional store).

    Always: ``sk_read_skill`` (Tier-2 full body) and ``sk_write_skill``.
    When a ``store`` is supplied, also ``sk_search_skill`` — a semantic search
    over the skill index built from the same shared discovery factory as
    ``tool_search``, so the agent can actively find a skill beyond the
    per-turn relevant-skills injection.
    """

    @tool
    def sk_read_skill(name: str) -> str:
        """Load the full body of a skill by its name.

        Use this when the skills index shows a skill that is relevant to the
        current task. Returns the full SKILL.md content (instructions + context).
        Example: sk_read_skill('pdf-to-archive')
        """
        return registry.get_body(name)

    @tool
    async def sk_write_skill(name: str, description: str, body: str) -> str:
        """Save a reusable task pattern as a personal skill.

        Call this after completing a task whose pattern is NOT already in the
        skills index. Keep the body concise (≤ 150 words): what was done,
        which tools were used, any gotchas.

        Args:
            name:        Short hyphenated identifier, e.g. 'pdf-to-archive'.
            description: One sentence — what + when to use. Max 512 chars.
            body:        Compact markdown instructions. Max 150 words.
        """
        try:
            slug = registry.write_skill(name=name, description=description, body=body)
        except Exception as exc:
            return f"error saving skill {name!r}: {exc}"
        # Index into the semantic store so it's retrievable later. Best-effort:
        # the disk file is authoritative; a store failure must not lose the skill.
        if store is not None:
            meta = registry.get_meta(slug)
            if meta is not None:
                try:
                    await store.upsert(meta, registry.get_body(slug))
                except Exception:
                    logger.warning(
                        "skills: wrote %r to disk but failed to index it", slug,
                        exc_info=True,
                    )
        return f"skill {slug!r} saved to personal scope"

    tools: list[BaseTool] = [sk_read_skill, sk_write_skill]

    if store is not None:
        from yuyutsava.discovery import VectorStoreProvider, make_discovery_search_tool

        provider = VectorStoreProvider(store, loader=registry.get_body, group="skill")
        tools.append(
            make_discovery_search_tool(
                provider,
                name="sk_search_skill",
                noun="skill",
                examples=(
                    "Returns the names + descriptions of the closest skills; then "
                    "call sk_read_skill('<name>') to load the full body."
                ),
            )
        )

    return tools


def make_read_skill_tool(registry: SkillRegistry) -> BaseTool:
    """Return only sk_read_skill (for subagents that can read but not write skills)."""
    return make_skill_tools(registry)[0]
