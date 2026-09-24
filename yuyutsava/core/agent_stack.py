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
from dataclasses import dataclass
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


def _install_board_stores(pg_pool: Any | None, embedder: Any | None) -> None:
    """Point the process-wide board, visual and note stores at Postgres.

    The todo_* capture tools must use the SAME board the daemon serves. With a
    pool the Pg store is primary (``get_default_todo_store()`` would otherwise
    lazily fall back to the SQLite twin and split the board); without one the
    lazy SQLite fallback is correct, so nothing is set.
    """
    if pg_pool is None:
        return
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


async def _refresh_prices(settings: LlmSettings) -> None:
    """Cache the active model's live price so the cost ledger is not $0.

    Fetched from the provider into ``~/.yuyutsava/model_prices.json`` (and so
    Langfuse prices it too). Off the loop, TTL-cached, never fatal.
    """
    try:
        await asyncio.to_thread(refresh_price_cache, settings)
    except Exception:  # noqa: BLE001 — pricing must never block a stack build
        logger.debug("price cache refresh failed", exc_info=True)


@dataclass(frozen=True)
class _Background:
    """The background-subagent host this stack uses; all ``None`` when off."""

    subagents: list[Any] | None = None
    host_url: str | None = None
    #: ``None`` when attached to a host another process owns.
    host: AsyncSubagentHost | None = None
    attachment: Any | None = None
    mirror: AsyncTaskMirror | None = None


async def _start_background(
    settings: LlmSettings,
    checkpointer: BaseCheckpointSaver,
    subagents: list[Any],
    stores: BackgroundStores,
) -> _Background:
    """Own or attach to the shared background-subagent host.

    First-come-wins: if a daemon (or another chat) already runs the LangGraph
    dev server, attach to its URL instead of starting a second one. The
    interactive CLI REPL has its own (intentional) blocking I/O, so it stays
    permissive by default; ``YUYUTSAVA_ALLOW_BLOCKING`` still overrides.
    """
    model = chat_model(settings)
    allow_blocking = resolve_allow_blocking(default=True)

    def _build_host() -> AsyncSubagentHost:
        return build_async_host(
            subagents,
            model=model,
            checkpointer=checkpointer,
            allow_blocking=allow_blocking,
            stores=stores,
        )

    attachment = await asyncio.to_thread(acquire_or_attach_host, factory=_build_host)
    if attachment.host is not None:
        logger.info(
            "CLI async host: owner @ %s graphs=%s",
            attachment.url,
            attachment.host.graph_ids,
        )
    else:
        logger.info("CLI async host: attached to running owner @ %s", attachment.url)
    return _Background(
        subagents=subagents,
        host_url=attachment.url,
        host=attachment.host,
        attachment=attachment,
        mirror=AsyncTaskMirror(),
    )


def _general_purpose_agent(
    workspace: Path,
    layout: WorkspaceLayout,
    skill_registry: SkillRegistry,
    search_config: SearchConfig,
    memory_store: Any | None,
    skill_store: Any | None,
) -> tuple[GeneralPurposeAgent, ConsentRegistry]:
    """The general-purpose subagent, and the consent registry its tools ask.

    Passing it makes deepagents name-match-override its built-in
    general-purpose with our tighter prompt and lazy tool discovery. Consent is
    session-scoped here (no events store; PROJECT grants persist only in the
    daemon path) and is installed as the process default for the tr_* tools.
    """
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
    return general_purpose, consent_registry


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
    stack (see :class:`yuyutsava.conversation.ConversationService`). With
    ``YUYUTSAVA_ASYNC_SUBAGENTS=1`` it also owns or attaches to the background
    host (:func:`_start_background`).
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

    # Long-term memory + skill retrieval. On the Postgres backend this stack
    # owns its own pool, so memory and skills get *semantic* recall via
    # pgvector; otherwise both use the SQLite keyword twins. Memory defaults ON
    # here — the longest-lived threads in the system — but
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

    # Token accounting. The daemon passes its own store; standalone, build one,
    # or a CLI session writes no llm_usage rows at all. The rows are the same
    # table the daemon writes, so /usage and the daemon's ledger agree.
    if usage_store is None:
        usage_store = _factory.usage()

    _install_board_stores(pg_pool, embedder)
    await _refresh_prices(settings)

    # Per-conversation transcript recall (Postgres only): index each turn so a
    # resumed session recalls prior topics after its checkpoint is swept. None on
    # the SQLite fallback, which the middleware treats as a no-op.
    transcript_index = _ctx.transcript_index

    # Materialize <ws>/.yuyutsava (sync mkdir → off-loop; see storage.paths).
    # Idempotent, so the daemon's ConversationManager building this same stack
    # lazily is harmless.
    await asyncio.to_thread(layout.ensure)
    general_purpose, consent_registry = _general_purpose_agent(
        workspace, layout, skill_registry, search_config, memory_store, skill_store
    )

    # MCP: the daemon passes its manager; standalone, start our own from the
    # merged global + workspace config (None when nothing is configured). The
    # bundle owns — and stops — only the one started here.
    owned_mcp = None
    if mcp_manager is None:
        owned_mcp = await start_manager_for_workspace(workspace, factory=mcp_manager_factory)
        mcp_manager = owned_mcp

    background = _Background()
    if _async_enabled():
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
        background = await _start_background(
            settings,
            checkpointer,
            [general_purpose, tinker_sub],
            BackgroundStores(
                artifact_store=artifact_store,
                context_settings=context_settings,
                summary_store=summary_store,
                memory_store=memory_store,
            ),
        )

    bundle = build_cli_deepagent(
        workspace,
        settings,
        deps=MasterDeps(
            bash_timeout_sec=bash_timeout_sec,
            permission_check=permission_check,
            search_config=search_config,
            checkpointer=checkpointer,
            subagents=[general_purpose, *(extra_subagents or [])],
            async_subagents=background.subagents,
            async_host_url=background.host_url,
            async_task_mirror=background.mirror,
            async_host=background.host,
            async_host_attachment=background.attachment,
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
