"""
LangChain tools that expose the SkillRegistry to agents.

read_skill  — load a full SKILL.md body on demand (agent-invoked)
write_skill — save a compact pattern to personal scope (orchestrator-only)
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from yuyutsava.skills.registry import SkillRegistry


def make_skill_tools(registry: SkillRegistry) -> list[BaseTool]:
    """Return [sk_read_skill, sk_write_skill] bound to registry."""

    @tool
    def sk_read_skill(name: str) -> str:
        """Load the full body of a skill by its name.

        Use this when the skills index shows a skill that is relevant to the
        current task. Returns the full SKILL.md content (instructions + context).
        Example: sk_read_skill('pdf-to-archive')
        """
        return registry.get_body(name)

    @tool
    def sk_write_skill(name: str, description: str, body: str) -> str:
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
            registry.write_skill(name=name, description=description, body=body)
            return f"skill {name!r} saved to personal scope"
        except Exception as exc:
            return f"error saving skill {name!r}: {exc}"

    return [sk_read_skill, sk_write_skill]


def make_read_skill_tool(registry: SkillRegistry) -> BaseTool:
    """Return only sk_read_skill (for subagents that can read but not write skills)."""
    return make_skill_tools(registry)[0]
