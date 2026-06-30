"""Factory for the CLI's agent stack.

A single function builds everything the chat REPL needs: skill registry,
task runner, general-purpose subagent, and the compiled deepagent bundle.

Pulled out of cli.py so tests, scripts, and any future second entry-point
can share one construction path instead of copy-pasting 30 lines of wiring.

Async (background) subagents are env-gated for v1: set ``YUYUTSAVA_ASYNC_SUBAGENTS=1``
to wire up an in-process ``AsyncSubagentHost`` + watcher + ``CliHitlBridge``.
``AgentBundle.async_host`` / ``async_task_mirror`` carry the live objects so
the REPL can drain bridge events and tear down on exit.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver

from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.task_runner.tools import set_default_consent
from yuyutsava.consent import ConsentRegistry
from yuyutsava.context.artifacts import SqliteArtifactStore
from yuyutsava.context.config import ContextSettings
from yuyutsava.context.summary_store import SqliteThreadSummaryStore
from yuyutsava.context.transcript_store import SqliteTranscriptStore
from yuyutsava.core.config import DockerSettings, LlmSettings, LocalSettings, SearchConfig, _env
from yuyutsava.core.engine import AgentBundle, build_cli_deepagent
from yuyutsava.core.llm import chat_model
from yuyutsava.core.config import llm_settings_from_env
from yuyutsava.memory.config import MemorySettings
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.storage.paths import state_db_path

logger = logging.getLogger("yuyutsava.cli.agent_stack")


def _async_enabled() -> bool:
    return os.environ.get("YUYUTSAVA_ASYNC_SUBAGENTS", "").lower() in ("1", "true", "yes")


async def _build_retrieval_stores(skill_registry: SkillRegistry):
    """Construct the memory + skill stores for the CLI.

    Returns ``(memory_store, skill_store, pg_pool, embedder)``. On the Postgres
    backend the CLI opens its own pool (returned for teardown) and builds the
    pgvector stores for true semantic recall; on any failure (or the SQLite
    backend) it falls back to the keyword twins in ``state.db``. After the
    stores exist, on-disk skills are indexed so a skill saved in a previous
    session is retrievable now, and NULL-embedding rows are backfilled.
    """
    from yuyutsava.skills.store import SkillIndexer
    from yuyutsava.storage.backend import StorageSettings

    storage = StorageSettings.from_env()
    mem_settings = MemorySettings.from_env(default_enabled=True)

    memory_store = None
    skill_store = None
    pg_pool = None
    embedder = None

    if storage.is_postgres():
        from yuyutsava.memory.embedder import Embedder
        from yuyutsava.memory.store import PgMemoryStore
        from yuyutsava.skills.store import PgSkillStore
        from yuyutsava.storage.pg import migrations as pg_migrations
        from yuyutsava.storage.pg.pool import PgPool

        try:
            pg_pool = PgPool(storage)
            await pg_pool.open()
            await pg_migrations.apply(pg_pool)
            embedder = Embedder(mem_settings)
            if not await embedder.healthcheck():
                logger.warning(
                    "CLI: embedder unreachable — memory/skills degrade to "
                    "keyword search until it recovers"
                )
            skill_store = PgSkillStore(pg_pool, embedder, min_score=mem_settings.min_score)
            if mem_settings.enabled:
                memory_store = PgMemoryStore(
                    pg_pool, embedder,
                    min_score=mem_settings.min_score,
                    dedup_threshold=mem_settings.dedup_threshold,
                )
        except Exception:
            logger.warning(
                "CLI: Postgres unavailable — falling back to SQLite stores",
                exc_info=True,
            )
            if embedder is not None:
                await embedder.aclose()
            if pg_pool is not None:
                await pg_pool.close()
            memory_store = skill_store = pg_pool = embedder = None

    if skill_store is None:
        from yuyutsava.skills.store import SqliteSkillStore
        skill_store = SqliteSkillStore(state_db_path())
    if memory_store is None and mem_settings.enabled:
        from yuyutsava.memory.store import SqliteMemoryStore
        memory_store = SqliteMemoryStore(state_db_path())

    # Catch the store up to on-disk skills (previous sessions, bundled,
    # workspace) so they're retrievable this session — best-effort.
    try:
        await SkillIndexer.sync(skill_registry, skill_store)
    except Exception:
        logger.warning("CLI: skill index sync failed", exc_info=True)

    # Re-embed any rows that landed without a vector (Pg only).
    if pg_pool is not None and embedder is not None:
        for store in (memory_store, skill_store):
            backfill = getattr(store, "backfill_embeddings", None)
            if backfill is not None:
                try:
                    await backfill()
                except Exception:
                    logger.warning("CLI: embedding backfill failed", exc_info=True)

    return memory_store, skill_store, pg_pool, embedder


async def build_agent_stack(
    workspace: Path,
    settings: LlmSettings,
    *,
    bash_timeout_sec: int,
    execution_mode: Literal["local", "docker"],
    docker_settings: DockerSettings,
    local_settings: LocalSettings,
    permission_check: bool,
    search_config: SearchConfig,
    checkpointer: BaseCheckpointSaver,
) -> AgentBundle:
    """Build the conversational deepagent + its subagent stack.

    Not CLI-specific despite the historical name: this is the same stack the
    daemon-hosted text/voice conversations build (see
    :class:`yuyutsava.conversation.ConversationService`). The legacy alias
    ``build_cli_agent_stack`` is kept below for existing callers.

    The current sync subagent list is just ``GeneralPurposeAgent`` — passing
    it causes deepagents to name-match-override its built-in default with our
    tighter prompt + lazy tool discovery via ToolRegistry.

    When ``YUYUTSAVA_ASYNC_SUBAGENTS=1``: also stand up an in-process
    ``AsyncSubagentHost`` hosting the same subagent(s) under ``-bg`` names,
    plus an ``AsyncTaskMirror``, an ``AsyncTaskHealthWatcher``, and a
    ``CliHitlBridge`` for routing interrupts to stdin.
    """
    # Context controller: CLI chat threads are the longest-lived in the
    # system, so offload + compaction are always on. Stores are the SQLite
    # twins in state.db regardless of YUYUTSAVA_STORAGE_BACKEND — the CLI
    # has no pool lifecycle owner yet (the daemon does); checkpoints still
    # honor the postgres backend via build_checkpointer.
    artifact_store = SqliteArtifactStore(state_db_path())
    summary_store = SqliteThreadSummaryStore(state_db_path())
    transcript_store = SqliteTranscriptStore(state_db_path())
    context_settings = ContextSettings.from_env(
        "cli", provider=_env("LLM_PROVIDER", None, "groq"),
    )
    compaction_model = chat_model(llm_settings_from_env("compaction"), temperature=0.0)

    skill_registry = SkillRegistry(workspace_dir=workspace)

    # Long-term memory + skill retrieval. On the Postgres backend the CLI owns
    # its own pool (the daemon owns one; the CLI didn't until now) so memory and
    # skills get *semantic* recall via pgvector; otherwise both fall back to the
    # SQLite keyword twins. Memory defaults ON in the CLI — these are the
    # longest-lived threads in the system — but an explicit
    # YUYUTSAVA_MEMORY_ENABLED=0 still disables it.
    memory_store, skill_store, pg_pool, embedder = await _build_retrieval_stores(
        skill_registry
    )
    sandbox_root_for_tr = (
        local_settings.sandbox_dir.resolve()
        if local_settings.sandbox_dir is not None
        else (workspace / "_sandbox").resolve()
    )
    # Consent (allowlist) registry — session-scoped only in standalone CLI mode
    # (no events store here; PROJECT grants persist only in the daemon path).
    consent_registry = ConsentRegistry()
    set_default_consent(consent_registry)
    task_runner = TaskRunnerAgent(
        workspace_root=workspace,
        sandbox_root=sandbox_root_for_tr,
        consent=consent_registry,
    )
    general_purpose = GeneralPurposeAgent(
        task_runner=task_runner,
        skill_registry=skill_registry,
        search_config=search_config,
    )

    async_subagents = None
    async_host_url = None
    async_host = None
    async_host_attachment = None
    async_mirror = None

    if _async_enabled():
        # Local imports keep langgraph_api off the import path when async is off.
        from yuyutsava.async_subagents.host import (
            AsyncSubagentHost,
            resolve_allow_blocking,
        )
        from yuyutsava.async_subagents.host_lock import acquire_or_attach_host
        from yuyutsava.async_subagents.mirror import AsyncTaskMirror

        model = chat_model(settings)
        async_subagents = [general_purpose]

        # The interactive CLI REPL has its own (intentional) blocking I/O, so it
        # stays permissive by default; YUYUTSAVA_ALLOW_BLOCKING still overrides.
        allow_blocking = resolve_allow_blocking(default=True)

        # First-come-wins shared host. If a daemon (or another chat) is
        # already running and owns the LangGraph dev server, attach to its
        # URL instead of starting a second one.
        def _build_host() -> AsyncSubagentHost:
            return AsyncSubagentHost.from_subagents(
                async_subagents,
                model=model,
                checkpointer=checkpointer,
                allow_blocking=allow_blocking,
            )

        attachment = await asyncio.to_thread(
            acquire_or_attach_host, factory=_build_host
        )
        async_host_attachment = attachment
        async_host_url = attachment.url
        async_host = attachment.host  # None when attached to another owner
        async_mirror = AsyncTaskMirror()
        if attachment.host is not None:
            logger.info(
                "CLI async host: owner @ %s graphs=%s",
                async_host_url, attachment.host.graph_ids,
            )
        else:
            logger.info("CLI async host: attached to running owner @ %s", async_host_url)

    bundle = build_cli_deepagent(
        workspace,
        settings,
        bash_timeout_sec=bash_timeout_sec,
        execution_mode=execution_mode,
        docker_settings=docker_settings,
        local_settings=local_settings,
        permission_check=permission_check,
        search_config=search_config,
        checkpointer=checkpointer,
        subagents=[general_purpose],
        async_subagents=async_subagents,
        async_host_url=async_host_url,
        async_task_mirror=async_mirror,
        async_host=async_host,
        async_host_attachment=async_host_attachment,
        artifact_store=artifact_store,
        summary_store=summary_store,
        memory_store=memory_store,
        transcript_store=transcript_store,
        context_settings=context_settings,
        compaction_model=compaction_model,
        skill_store=skill_store,
    )
    # Hand the CLI-owned pool + embedder to the bundle so teardown closes them.
    bundle.pg_pool = pg_pool
    bundle.embedder = embedder
    return bundle


# Back-compat alias: the stack is no longer CLI-only (the daemon hosts text +
# voice conversations on the same builder), but existing imports keep working.
build_cli_agent_stack = build_agent_stack
