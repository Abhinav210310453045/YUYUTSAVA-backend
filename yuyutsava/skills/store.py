"""Skill store: pgvector semantic search over SKILL.md, SQLite keyword twin.

The on-disk ``SKILL.md`` files (see :class:`~yuyutsava.skills.registry.SkillRegistry`)
remain the source of truth — portable, git-committable, human-editable. This
store is the *search index*: it embeds a skill's description on write so the
agent can retrieve only the skills relevant to the task at hand instead of
having every skill dumped into the system prompt.

Built on the shared :mod:`yuyutsava.retrieval` engine — the exact same pgvector
cosine/keyword/backfill machinery as memory. Write contract matches memory:
``upsert`` stores a skill even if embedding fails (still keyword-findable; a
backfill re-embeds on recovery), and the disk file is authoritative regardless.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from yuyutsava.memory.embedder import Embedder
from yuyutsava.retrieval.hit import Hit
from yuyutsava.retrieval.keyword import keyword_tokens
from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable
from yuyutsava.retrieval.vector import vector_literal
from yuyutsava.skills.registry import SkillMeta
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.pg.pool import PgPool

logger = logging.getLogger("yuyutsava.skills.store")

# Column map for the shared pgvector engine. scope/agent/name ride in payload.
_SKILLS_TABLE = PgVectorTable(
    table="skills",
    id_col="name",
    text_col="description",
    extra_cols=("scope", "agent", "name"),
    created_col="updated_ts",
)


class SkillStore(ABC):
    """Index + semantic retrieval for skills. Disk stays the source of truth."""

    @abstractmethod
    async def upsert(self, meta: SkillMeta, body: str) -> None:
        """Index (or re-index) one skill by name."""

    @abstractmethod
    async def search(self, query: str, k: int = 5, agent: str | None = None) -> list[Hit]:
        """Top-k skills relevant to ``query``, scoped to ``agent`` (None = all)."""

    @abstractmethod
    async def all_names(self) -> set[str]:
        """Names already indexed (used by the boot-time sync to find new ones)."""


def _agent_filter(agent: str | None) -> tuple[str, list]:
    """``agent IS NULL OR agent = %s`` — mirrors SkillRegistry.scan(agent)."""
    if agent:
        return "AND (agent IS NULL OR agent = %s)", [agent]
    return "", []



# NOTE: PgSkillStore was replaced on 2026-08-08 by UnifiedSkillStore in
# skills/store_unified.py (ADR-002 step 2.5b). The pgvector asymmetry is now a
# DECLARED capability (supports_semantic_search) rather than a getattr probe.
# Parity verified on both live backends in test/storage/test_skill_store_parity.py.


# NOTE: SqliteSkillStore was replaced on 2026-08-08 by UnifiedSkillStore in
# skills/store_unified.py (ADR-002 step 2.5b). The pgvector asymmetry is now a
# DECLARED capability (supports_semantic_search) rather than a getattr probe.
# Parity verified on both live backends in test/storage/test_skill_store_parity.py.

class SkillIndexer:
    """Indexes on-disk skills into a :class:`SkillStore` at boot.

    Disk is the source of truth; this catches the store up to it (skills written
    by a previous process, bundled skills, workspace skills committed to git).
    Idempotent and best-effort — a store outage must not break startup.
    """

    @staticmethod
    async def sync(registry, store: SkillStore) -> int:
        try:
            existing = await store.all_names()
        except Exception:
            logger.warning("skills: index sync skipped — store unavailable", exc_info=True)
            return 0
        count = 0
        for meta in registry.scan():
            if meta.name in existing:
                continue
            try:
                await store.upsert(meta, registry.get_body(meta.name))
                count += 1
            except Exception:
                logger.warning("skills: failed to index %r", meta.name, exc_info=True)
        if count:
            logger.info("skills: indexed %d on-disk skill(s) into the store", count)
        return count
