"""Factory for the chat agent stack.

A single function builds everything a chat conversation needs: skill registry,
task runner, general-purpose subagent, and the compiled deepagent bundle. The
CLI REPL and the daemon's text and voice conversations build the same stack
through it, so it lives in ``core`` rather than in either front end.

Async (background) subagents are env-gated for v1: set ``YUYUTSAVA_ASYNC_SUBAGENTS=1``
to wire up an in-process ``AsyncSubagentHost`` + watcher + ``CliHitlBridge``.
``AgentBundle.async_host`` / ``async_task_mirror`` carry the live objects so
the REPL can drain bridge events and tear down on exit.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver

from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.task_runner.tools import set_default_consent
from yuyutsava.agents.tinker.subagent import make_tinker_subagent
from yuyutsava.async_subagents.host import AsyncSubagentHost, resolve_allow_blocking
from yuyutsava.async_subagents.host_lock import acquire_or_attach_host
from yuyutsava.async_subagents.mirror import AsyncTaskMirror
from yuyutsava.config.env import env, env_flag
from yuyutsava.config.execution import DockerSettings, LocalSettings
from yuyutsava.config.search import SearchConfig
from yuyutsava.consent import ConsentRegistry
from yuyutsava.context.config import ContextSettings
from yuyutsava.core.async_host import BackgroundStores, build_async_host
from yuyutsava.core.deps import MasterDeps
from yuyutsava.core.engine import AgentBundle, build_cli_deepagent
from yuyutsava.core.stores import StoreFactory, build_retrieval_stores
from yuyutsava.llm.factory import chat_model
from yuyutsava.llm.pricing import refresh_price_cache
from yuyutsava.llm.settings import LlmSettings, llm_settings_from_env
from yuyutsava.mcp.loader import start_manager_for_workspace
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.paths import WorkspaceLayout
from yuyutsava.todoboard.recall import TodoNoteIndex, set_default_note_index
from yuyutsava.todoboard.store import set_default_todo_store
from yuyutsava.todoboard.store_unified import pg_todo_store
from yuyutsava.visuals.store import set_default_visual_store
from yuyutsava.visuals.store_unified import pg_visual_store

logger = logging.getLogger("yuyutsava.core.agent_stack")


def _async_enabled() -> bool:
    return env_flag("YUYUTSAVA_ASYNC_SUBAGENTS", true=("1", "true", "yes"))


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
    mcp_manager: Any | None = None,
    mcp_manager_factory: Any | None = None,
    usage_store: Any | None = None,
    budget_tokens: int | None = None,
    prefs_store: Any | None = None,
    runtime_settings: Any | None = None,
    cap_enforcer: Any | None = None,
    extra_subagents: "list[Any] | None" = None,
    extra_tools: "list[Any] | None" = None,
    front: str = "app",
) -> AgentBundle:
    """Build the conversational deepagent + its subagent stack.

    The trailing keyword group is daemon-supplied wiring (all default to
    None, keeping the standalone CLI's behavior unchanged): ``mcp_manager``
    scopes user-configured MCP tools to ``"cli"`` — when the daemon passes
    none, the stack starts its OWN from the merged global + workspace
    ``mcp_config.json`` (``start_manager_for_workspace``; ``None`` when
    nothing is configured) and hands it to the bundle, which stops it in
    ``aclose``; ``mcp_manager_factory`` is that helper's test seam; ``usage_store`` /
    ``budget_tokens`` attach the orchestrator's UsagePolicy/BudgetPolicy pair;
    ``prefs_store`` adds the per-turn USER PREFERENCES injector;
    ``runtime_settings`` carries the user's dedicated-subagent switches (this
    bundle is cached across conversations, so the toggle is enforced per call
    rather than baked into the roster); ``cap_enforcer`` rate-caps ws_*
    searches; ``extra_subagents`` join the sync ``task`` roster next to
    general-purpose; ``extra_tools`` are one-off
    daemon tools (e.g. orch_submit).

    The CLI REPL and the daemon-hosted text/voice conversations build this same
    stack (see :class:`yuyutsava.conversation.ConversationService`).

    The current sync subagent list is just ``GeneralPurposeAgent`` — passing
    it causes deepagents to name-match-override its built-in default with our
    tighter prompt + lazy tool discovery via ToolRegistry.

    When ``YUYUTSAVA_ASYNC_SUBAGENTS=1``: also stand up an in-process
    ``AsyncSubagentHost`` hosting the same subagent(s) under ``-bg`` names,
    plus an ``AsyncTaskMirror``, an ``AsyncTaskHealthWatcher``, and a
    ``CliHitlBridge`` for routing interrupts to stdin.
    """
    # Context controller: CLI chat threads are the longest-lived in the
    # system, so offload + compaction are always on.
    context_settings = ContextSettings.from_env(
        "cli",
        provider=env("LLM_PROVIDER", None, "groq"),
    )
    compaction_model = chat_model(llm_settings_from_env("compaction"), temperature=0.0)

    # Every per-workspace path (sandbox, outputs, skills, scratch) comes from
    # the one layout helper; the explicit --sandbox-dir/--output-dir overrides
    # ride through it rather than around it.
    layout = WorkspaceLayout.for_workspace(
        workspace,
        sandbox_override=local_settings.sandbox_dir,
        outputs_override=local_settings.output_dir,
    )
    skill_registry = SkillRegistry(workspace_dir=layout.skills)

    # Long-term memory + skill retrieval. On the Postgres backend the CLI owns
    # its own pool (the daemon owns one; the CLI didn't until now) so memory and
    # skills get *semantic* recall via pgvector; otherwise both fall back to the
    # SQLite keyword twins. Memory defaults ON in the CLI — these are the
    # longest-lived threads in the system — but an explicit
    # YUYUTSAVA_MEMORY_ENABLED=0 still disables it.
    memory_store, skill_store, pg_pool, embedder = await build_retrieval_stores(skill_registry)

    # Context-controller stores, selected ONCE by StoreFactory — the same
    # selection the tinker bundle and the daemon make, so they cannot drift.
    # See StoreFactory.context_stores for why these stay PG-primary /
    # SQLite-fallback rather than RoutedStore.
    _factory = StoreFactory(StorageSettings.from_env(), pg_pool=pg_pool, embedder=embedder)
    _ctx = _factory.context_stores(semantic_recall=context_settings.semantic_recall)
    artifact_store = _ctx.artifacts
    summary_store = _ctx.summaries
    transcript_store = _ctx.transcripts

    # Token accounting. The kwarg exists for the daemon to pass its own store,
    # but nothing filled it in standalone mode, so a CLI session wrote zero
    # llm_usage rows — the 12 Sep post-mortem had to reconstruct 4M input
    # tokens from usage_metadata on recorded messages, and only because the
    # transcript happened to be on. The rows are the same table the daemon
    # writes, so /usage and the daemon's ledger agree.
    if usage_store is None:
        usage_store = _factory.usage()

    # TODO board: point the todo_* capture tools at the SAME board the daemon
    # serves. With a pool the Pg store is primary (get_default_todo_store()
    # would otherwise lazily fall back to the SQLite twin and split the board);
    # without one the lazy SQLite fallback is correct, so leave it unset.
    if pg_pool is not None:
        set_default_todo_store(pg_todo_store(pg_pool))
        # Same split, same fix, for vis_* images: without this the lazy getter
        # falls back to the SQLite twin, so a chart made in the CLI lands in
        # state.db while the daemon and the app read visual_artifacts in
        # Postgres — and vis_list_artifacts shows two different worlds.
        set_default_visual_store(pg_visual_store(pg_pool))
        # Board-note recall: embed-on-write for notes authored through this
        # stack + todo_recall searches. Boot backfill is the daemon's job —
        # a CLI start stays light.
        if embedder is not None:
            set_default_note_index(TodoNoteIndex(pg_pool, embedder=embedder))

    # Best-effort: fetch the active model's live price from its provider and cache
    # it into ~/.yuyutsava/model_prices.json so the cost ledger (and Langfuse)
    # price this model correctly instead of falling to $0. Off the loop, TTL-cached,
    # never fatal.
    try:
        await asyncio.to_thread(refresh_price_cache, settings)
    except Exception:  # noqa: BLE001 — pricing must never block a stack build
        logger.debug("price cache refresh failed", exc_info=True)

    # Per-conversation transcript recall (Postgres only): index each turn so a
    # resumed session recalls prior topics after its checkpoint is swept. None on
    # the SQLite fallback, which the middleware treats as a no-op.
    transcript_index = _ctx.transcript_index

    # Materialize <ws>/.yuyutsava (sync mkdir → off-loop; see storage.paths).
    # Idempotent, so the daemon's ConversationManager building this same stack
    # lazily is harmless.
    await asyncio.to_thread(layout.ensure)
    # Consent (allowlist) registry — session-scoped only in standalone CLI mode
    # (no events store here; PROJECT grants persist only in the daemon path).
    consent_registry = ConsentRegistry()
    set_default_consent(consent_registry)
    task_runner = TaskRunnerAgent(
        workspace_root=workspace,
        sandbox_root=layout.sandbox,
        consent=consent_registry,
    )
    general_purpose = GeneralPurposeAgent(
        task_runner=task_runner,
        skill_registry=skill_registry,
        can_write_skills=True,
        search_config=search_config,
        memory_store=memory_store,
        skill_store=skill_store,
    )

    # MCP: the daemon passes its manager; standalone, start our own from the
    # merged global + workspace config (None when nothing is configured). The
    # bundle owns — and stops — only the one started here.
    owned_mcp = None
    if mcp_manager is None:
        owned_mcp = await start_manager_for_workspace(workspace, factory=mcp_manager_factory)
        mcp_manager = owned_mcp

    async_subagents = None
    async_host_url = None
    async_host = None
    async_host_attachment = None
    async_mirror = None

    if _async_enabled():
        model = chat_model(settings)
        # The background TinkerAgent rides along as an async-only peer so the
        # conversational master can delegate "tinker on card X in the
        # background" — async-only: interactive tinkering has its own per-card
        # bundle. It gets the same MCP manager as the master (scope "tinker");
        # its graph runs on the host's loop, and mcp/tool_adapter marshals
        # calls back to the manager's loop.
        tinker_sub = make_tinker_subagent(
            skill_registry=skill_registry,
            search_config=search_config,
            memory_store=memory_store,
            skill_store=skill_store,
            mcp_manager=mcp_manager,
            consent=consent_registry,
        )
        async_subagents = [general_purpose, tinker_sub]

        # The interactive CLI REPL has its own (intentional) blocking I/O, so it
        # stays permissive by default; YUYUTSAVA_ALLOW_BLOCKING still overrides.
        allow_blocking = resolve_allow_blocking(default=True)

        # First-come-wins shared host. If a daemon (or another chat) is
        # already running and owns the LangGraph dev server, attach to its
        # URL instead of starting a second one.
        def _build_host() -> AsyncSubagentHost:
            return build_async_host(
                async_subagents,
                model=model,
                checkpointer=checkpointer,
                allow_blocking=allow_blocking,
                stores=BackgroundStores(
                    artifact_store=artifact_store,
                    context_settings=context_settings,
                    summary_store=summary_store,
                    memory_store=memory_store,
                ),
            )

        attachment = await asyncio.to_thread(acquire_or_attach_host, factory=_build_host)
        async_host_attachment = attachment
        async_host_url = attachment.url
        async_host = attachment.host  # None when attached to another owner
        async_mirror = AsyncTaskMirror()
        if attachment.host is not None:
            logger.info(
                "CLI async host: owner @ %s graphs=%s",
                async_host_url,
                attachment.host.graph_ids,
            )
        else:
            logger.info("CLI async host: attached to running owner @ %s", async_host_url)

    bundle = build_cli_deepagent(
        workspace,
        settings,
        deps=MasterDeps(
            bash_timeout_sec=bash_timeout_sec,
            permission_check=permission_check,
            search_config=search_config,
            checkpointer=checkpointer,
            subagents=[general_purpose, *(extra_subagents or [])],
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
            transcript_index=transcript_index,
            mcp_tools=(mcp_manager.tools_for("cli") if mcp_manager is not None else None),
            cap_enforcer=cap_enforcer,
            budget_tokens=budget_tokens,
            usage_store=usage_store,
            prefs_store=prefs_store,
            runtime_settings=runtime_settings,
            extra_tools=extra_tools,
        ),
        execution_mode=execution_mode,
        docker_settings=docker_settings,
        local_settings=local_settings,
        front=front,
    )
    # Hand the CLI-owned pool + embedder (+ MCP manager, when this stack started
    # one) to the bundle so teardown closes them.
    bundle.pg_pool = pg_pool
    bundle.embedder = embedder
    bundle.mcp_manager = owned_mcp
    # Not owned (nothing to close) — exposed so the front can report what the
    # turn cost, reading the same rows the graph just wrote.
    bundle.usage_store = usage_store
    return bundle


# Back-compat alias: the stack is not CLI-only (the daemon hosts text + voice
# conversations on the same builder), but existing imports keep working.
