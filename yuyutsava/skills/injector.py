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

import logging
import re

from yuyutsava.core.config import LIMITS
from yuyutsava.retrieval.hit import Hit
from yuyutsava.retrieval.injector import RetrievalInjector
from yuyutsava.skills.store import SkillStore

logger = logging.getLogger("yuyutsava.skills.injector")

_PREFIX = (
    "RELEVANT SKILLS (matched to this task; read the full body with "
    "sk_read_skill before using):"
)


def _render(h: Hit) -> str:
    return f"  - {h.payload.get('name', h.id)}: {h.text}"


# Names from the most recent injection, newest last. Retrieval happens deep
# inside a graph run where nothing is returned to the caller, so "which skills
# did it actually see?" was unanswerable — and a session that re-derived two
# procedures from scratch while 31 skills sat indexed is exactly when you want
# to know. Process-global and purely diagnostic: the ``/skills`` command reads
# it, nothing branches on it.
_last_recalled: tuple[str, ...] = ()

#: Matches the line ``_render`` above produces. Parser and format live
#: together on purpose — one is the other's only reader.
_LINE = re.compile(r"^ {2}- ([^:]+):")


def last_recalled() -> tuple[str, ...]:
    """Skill names from the most recent per-turn injection (may be empty)."""
    return _last_recalled


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
        global _last_recalled
        block = await self._inner.build_block(task_text)
        names = tuple(
            m.group(1).strip() for m in (_LINE.match(ln) for ln in block.splitlines()) if m
        )
        _last_recalled = names
        if names:
            logger.info("skills recalled for this turn: %s", ", ".join(names))
        else:
            logger.debug("skills: nothing matched this turn")
        return block
