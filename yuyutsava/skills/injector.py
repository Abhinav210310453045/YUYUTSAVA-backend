"""Build the RELEVANT SKILLS block injected into an agent prompt.

The semantic counterpart to the old ``SkillRegistry.index_block()``: instead of
dumping *every* skill into the system prompt at build time (which doesn't scale
and can't be matched to the task), this retrieves the top-k skills relevant to
the current task text and renders only those. Read the full body on demand with
``sk_read_skill``.

A thin wrapper over the generic
:class:`~yuyutsava.retrieval.injector.RetrievalInjector`; never raises.
"""

from __future__ import annotations

from yuyutsava.core.config import LIMITS
from yuyutsava.retrieval.hit import Hit
from yuyutsava.retrieval.injector import RetrievalInjector
from yuyutsava.skills.store import SkillStore

_PREFIX = (
    "RELEVANT SKILLS (matched to this task; read the full body with "
    "sk_read_skill before using):"
)


def _render(h: Hit) -> str:
    return f"  - {h.payload.get('name', h.id)}: {h.text}"


class SkillInjector:
    """Renders top-k task-relevant skills into a prompt block."""

    def __init__(
        self, store: SkillStore, *, agent: str | None = None, top_k: int = 5
    ) -> None:
        self._inner = RetrievalInjector(
            store,  # SkillStore.search duck-types VectorStore.search(query, k, agent)
            top_k=top_k,
            prefix=_PREFIX,
            budget_chars=LIMITS.max_skill_index_chars,
            render=_render,
            search_kwargs={"agent": agent},
        )

    async def build_block(self, task_text: str) -> str:
        """Return the skills block string, or empty string. Never raises."""
        return await self._inner.build_block(task_text)
