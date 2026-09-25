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
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.tinker.subagent import make_tinker_subagent
from yuyutsava.config.execution import ExecutionSettings
from yuyutsava.config.search import SearchConfig
from yuyutsava.consent import ConsentRegistry
from yuyutsava.core.async_host import (
    Background,
    BackgroundStores,
    async_subagents_enabled,
    start_background,
)
from yuyutsava.core.deps import MasterDeps, StackWiring
from yuyutsava.core.engine import AgentBundle, build_cli_deepagent
from yuyutsava.core.stores import build_master_stores, standalone_board
from yuyutsava.llm.pricing import refresh_price_cache
from yuyutsava.llm.settings import LlmSettings
from yuyutsava.mcp.loader import start_manager_for_workspace
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.storage.paths import WorkspaceLayout
from yuyutsava.visuals.store import set_default_visual_store
from yuyutsava.visuals.store_unified import pg_visual_store

logger = logging.getLogger("yuyutsava.core.agent_stack")


def _install_visual_store(pg_pool: Any | None) -> None:
    """Point the process-wide visual store at Postgres when there is a pool.

    Without this the lazy getter falls back to the SQLite twin, so a chart
    made in the CLI lands in state.db while the daemon and the app read
    visual_artifacts in Postgres — and vis_list_artifacts shows two worlds.
    """
    if pg_pool is not None:
        set_default_visual_store(pg_visual_store(pg_pool))


async def _refresh_prices(settings: LlmSettings) -> None:
    """Cache the active model's live price so the cost ledger is not $0.

    Fetched from the provider into ``~/.yuyutsava/model_prices.json`` (and so
    Langfuse prices it too). Off the loop, TTL-cached, never fatal.
    """
    try:
        await asyncio.to_thread(refresh_price_cache, settings)
    except Exception:  # noqa: BLE001 — pricing must never block a stack build
        logger.debug("price cache refresh failed", exc_info=True)


def _stack_consent(wiring: StackWiring) -> Any:
    """The consent registry every gateway in this stack asks.

    Under the daemon it is the daemon's, so a grant made in a chat is persisted
    and seen by every surface. Standalone, a session-scoped registry of its own
    (no events store to persist PROJECT grants into).
    """
    return wiring.consent if wiring.consent is not None else ConsentRegistry()


def _general_purpose_agent(
    task_runner: TaskRunnerAgent,
    skill_registry: SkillRegistry,
    search_config: SearchConfig,
    memory_store: Any | None,
    skill_store: Any | None,
) -> GeneralPurposeAgent:
    """The general-purpose subagent, on the stack's shared gateway.

    Passing it makes deepagents name-match-override its built-in
    general-purpose with our tighter prompt and lazy tool discovery.
    """
    return GeneralPurposeAgent(
        task_runner=task_runner,
        skill_registry=skill_registry,
        can_write_skills=True,
        search_config=search_config,
        memory_store=memory_store,
        skill_store=skill_store,
    )


async def build_agent_stack(
    workspace: Path,
    settings: LlmSettings,
    *,
    execution: ExecutionSettings,
    bash_timeout_sec: int,
    permission_check: bool,
    search_config: SearchConfig,
    checkpointer: BaseCheckpointSaver,
    wiring: StackWiring | None = None,
    front: str = "app",
) -> AgentBundle:
    """Build the conversational deepagent + its subagent stack.

    ``wiring`` is what the daemon supplies (see :class:`StackWiring`); absent,
    this is the standalone CLI. Without an ``mcp_manager`` the stack starts its
    OWN from the merged global + workspace ``mcp_config.json`` (``None`` when
    nothing is configured) and hands it to the bundle, which stops it in
    ``aclose``. ``usage_store`` / ``budget_tokens`` attach the UsagePolicy /
    BudgetPolicy pair; ``prefs_store`` adds the per-turn USER PREFERENCES
    injector; ``cap_enforcer`` rate-caps ws_* searches.

    The CLI REPL and the daemon-hosted text/voice conversations build this same
    stack (see :class:`yuyutsava.conversation.ConversationService`). With
    ``YUYUTSAVA_ASYNC_SUBAGENTS=1`` it also owns or attaches to the background
    host (:func:`yuyutsava.core.async_host.start_background`).
    """
    wiring = wiring or StackWiring()
    mcp_manager = wiring.mcp_manager
    usage_store = wiring.usage_store
    # Every per-workspace path (sandbox, outputs, skills, scratch) comes from
    # the one layout helper; the explicit --sandbox-dir/--output-dir overrides
    # ride through it rather than around it.
    layout = WorkspaceLayout.for_workspace(
        workspace,
        sandbox_override=execution.local.sandbox_dir,
        outputs_override=execution.local.output_dir,
    )
    skill_registry = SkillRegistry(workspace_dir=layout.skills)
    # CLI chat threads are the longest-lived in the system, so offload and
    # compaction are always on and memory defaults ON (YUYUTSAVA_MEMORY_ENABLED=0
    # still disables it).
    stores = await build_master_stores("cli", skill_registry)
    memory_store, skill_store = stores.memory_store, stores.skill_store
    pg_pool, embedder = stores.pg_pool, stores.embedder

    # Token accounting. The daemon passes its own store; standalone, build one,
    # or a CLI session writes no llm_usage rows at all. The rows are the same
    # table the daemon writes, so /usage and the daemon's ledger agree.
    if usage_store is None:
        usage_store = stores.factory.usage()

    _install_visual_store(pg_pool)
    # The daemon's board (routed store, boot-synced note index) when it hands
    # one over; standalone, the stack's own.
    board = wiring.board if wiring.board is not None else standalone_board(stores)
    await _refresh_prices(settings)

    # Materialize <ws>/.yuyutsava (sync mkdir → off-loop; see storage.paths).
    # Idempotent, so the daemon's ConversationManager building this same stack
    # lazily is harmless.
    await asyncio.to_thread(layout.ensure)
    consent = _stack_consent(wiring)
    # One gateway for the master and its general-purpose subagent: they share
    # its zones, policy, consent and — in docker mode — its container.
    task_runner = TaskRunnerAgent(
        workspace_root=workspace,
        sandbox_root=layout.sandbox,
        policy=wiring.policy,
        consent=consent,
    )
    general_purpose = _general_purpose_agent(
        task_runner, skill_registry, search_config, memory_store, skill_store
    )

    # MCP: the daemon passes its manager; standalone, start our own from the
    # merged global + workspace config (None when nothing is configured). The
    # bundle owns — and stops — only the one started here.
    owned_mcp = None
    if mcp_manager is None:
        owned_mcp = await start_manager_for_workspace(workspace, factory=wiring.mcp_manager_factory)
        mcp_manager = owned_mcp

    background = Background()
    if async_subagents_enabled():
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
            policy=wiring.policy,
            consent=consent,
            board=board,
        )
        background = await start_background(
            settings,
            checkpointer,
            [general_purpose, tinker_sub],
            BackgroundStores(
                artifact_store=stores.context.artifacts,
                context_settings=stores.context_settings,
                summary_store=stores.context.summaries,
                memory_store=memory_store,
            ),
        )
        if background.host is not None:
            logger.info(
                "CLI async host: owner @ %s graphs=%s",
                background.host_url,
                background.host.graph_ids,
            )
        else:
            logger.info("CLI async host: attached to running owner @ %s", background.host_url)

    bundle = build_cli_deepagent(
        workspace,
        settings,
        deps=MasterDeps(
            bash_timeout_sec=bash_timeout_sec,
            permission_check=permission_check,
            search_config=search_config,
            checkpointer=checkpointer,
            subagents=[general_purpose, *(wiring.extra_subagents or [])],
            async_subagents=background.subagents,
            async_host_url=background.host_url,
            async_task_mirror=background.mirror,
            async_host=background.host,
            async_host_attachment=background.attachment,
            artifact_store=stores.context.artifacts,
            summary_store=stores.context.summaries,
            memory_store=memory_store,
            transcript_store=stores.context.transcripts,
            context_settings=stores.context_settings,
            compaction_model=stores.compaction_model,
            skill_store=skill_store,
            # Per-conversation transcript recall (Postgres only); None on the
            # SQLite fallback, which the middleware treats as a no-op.
            transcript_index=stores.context.transcript_index,
            mcp_tools=(mcp_manager.tools_for("cli") if mcp_manager is not None else None),
            cap_enforcer=wiring.cap_enforcer,
            budget_tokens=wiring.budget_tokens,
            usage_store=usage_store,
            prefs_store=wiring.prefs_store,
            runtime_settings=wiring.runtime_settings,
            extra_tools=wiring.extra_tools,
            task_runner=task_runner,
            board=board,
        ),
        execution_mode=execution.mode,
        docker_settings=execution.docker,
        local_settings=execution.local,
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
