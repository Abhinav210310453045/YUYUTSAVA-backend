"""
Daemon subsystem wiring.

Splits the ~150 lines of inline constructor calls that used to live in
``daemon/main.py`` into one function whose output is a frozen
:class:`DaemonSubsystems` record. ``main.py`` keeps only the lifecycle —
signal handlers, task scheduling, electron launch, ordered teardown.

The single async :func:`build_daemon` entry point opens every store,
starts every long-lived subsystem (MCP, sources, checkpointer), and
returns a populated :class:`DaemonSubsystems`. The caller owns shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import uvicorn

from yuyutsava.agents.face_watcher.agent import FaceWatcherAgent
from yuyutsava.agents.file_organizer.agent import FileOrganizerAgent
from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
from yuyutsava.agents.orchestrator.agent import OrchestratorDeps
from yuyutsava.agents.orchestrator.capabilities import render_capabilities_block
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.task_runner.tools import set_default_policy
from yuyutsava.agents.triage.agent import TriageAgent
from yuyutsava.context.artifacts import PgArtifactStore, SqliteArtifactStore
from yuyutsava.context.config import ContextSettings
from yuyutsava.context.injector import MemoryInjector
from yuyutsava.context.summary_store import PgThreadSummaryStore, SqliteThreadSummaryStore
from yuyutsava.core.config import (
    DaemonConfig, EventsConfig, LlmSettings, SearchConfig, SourceConfig,
    _env, llm_settings_from_env,
)
from yuyutsava.core.llm import chat_model
from yuyutsava.core.policy import PermissionsPolicy, StorePolicyCapEnforcer
from yuyutsava.daemon.channels import ChannelEvent, ChannelRouter, TimelinePayload
from yuyutsava.daemon.checkpointing import CheckpointerSaver
from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop
from yuyutsava.daemon.terminal_channel import TerminalChannel
from yuyutsava.daemon.triage_loop import OrchestratorTask, TriageLoop
from yuyutsava.daemon.web.server import WebChannel, WebHub, make_app
from yuyutsava.events.bus import EventBus
from yuyutsava.events.registry import SourceRegistry
from yuyutsava.mcp.config import MCPConfig
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.memory.config import MemorySettings
from yuyutsava.memory.embedder import Embedder
from yuyutsava.memory.store import MemoryStore, PgMemoryStore, SqliteMemoryStore
from yuyutsava.prefs.injector import PrefsInjector
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.events import Store
from yuyutsava.storage.paths import blobs_dir, checkpoints_db_path, state_db_path, state_dir
from yuyutsava.storage.pg import migrations as pg_migrations
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.prefs import PrefsStore
from yuyutsava.storage.sweeper import BlobSweepTarget, SweeperConfig, UnifiedSweeper

logger = logging.getLogger("yuyutsava.daemon.bootstrap")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DaemonOptions:
    """Args-derived options for the daemon bootstrap.

    Kept separate from :class:`DaemonConfig` (env-derived) because these
    map 1:1 to CLI flags. ``main.py`` builds this from ``argparse`` and
    hands it to :func:`build_daemon`.
    """

    workspace: Path
    headless: bool      # --no-ui
    voice: bool         # --voice
    verbose: bool       # --verbose


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DaemonSubsystems:
    """Everything the daemon lifecycle needs after wiring is done."""

    # configs
    daemon_cfg: DaemonConfig
    home: Path
    workspace: Path

    # stores
    store: Store
    prefs_store: PrefsStore

    # bus + sources
    bus: EventBus
    registry: SourceRegistry

    # channels + web
    channels: ChannelRouter
    web_hub: WebHub | None
    web_server: uvicorn.Server | None
    web_url: str

    # external integrations
    mcp_manager: MCPClientManager

    # storage TTL
    checkpointer_saver: CheckpointerSaver
    sweeper: UnifiedSweeper

    # storage backend (postgres mode) + context controller. ``pg_pool`` and
    # ``embedder`` are owned here for teardown; the stores are borrowed by
    # OrchestratorDeps / the sweeper. All None/sqlite in zero-config mode.
    pg_pool: object | None
    artifact_store: object
    summary_store: object
    memory_store: object | None
    embedder: object | None

    # subagents + queue + loops
    task_queue: asyncio.Queue[OrchestratorTask]
    triage_loop: TriageLoop
    orch_loop: OrchestratorLoop

    # for logging / future use
    skill_registry: SkillRegistry
    triage_settings: LlmSettings
    orchestrator_settings: LlmSettings
    subagent_names: tuple[str, ...]

    # Async (background) subagent infrastructure. ``None`` when async is
    # disabled (e.g. ``--no-async-subagents`` or env-gated). Lifecycle owns
    # shutdown ordering: watcher first (cancels in-flight runs via SDK),
    # then host (uvicorn server thread).
    async_host: object | None = None
    async_task_mirror: object | None = None
    async_task_watcher: object | None = None
    session_origin: object | None = None
    # Profile-wide host attachment returned by ``acquire_or_attach_host``;
    # the daemon shutdown hook calls ``release_host_lock`` on it. Stays
    # ``None`` when the daemon attached to an already-running host owned
    # by another process (no lock to release) or when async subs are off.
    async_host_attachment: object | None = None

    # hot reload — closure over registry + daemon_cfg; reload-loop calls it.
    hot_reload_events_config: Callable[[], Awaitable[None]] = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inject_heartbeat(events_cfg: EventsConfig, heartbeat_sec: int) -> EventsConfig:
    """Inject ``heartbeat_sec`` into every source's params.

    Used at boot and at hot-reload time so live sources still sleep
    between bursts. Returning a new ``EventsConfig`` keeps the frozen
    dataclass invariant.
    """
    if heartbeat_sec <= 0:
        return events_cfg
    return EventsConfig(sources={
        name: SourceConfig(
            name=src.name,
            enabled=src.enabled,
            params={**src.params, "heartbeat_sec": heartbeat_sec},
        )
        for name, src in events_cfg.sources.items()
    })


def _build_initial_events_config(opts: DaemonOptions, daemon_cfg: DaemonConfig) -> EventsConfig:
    """Load events_config.json, fold in --voice, then inject heartbeat."""
    events_cfg = EventsConfig.from_file()
    if opts.voice and "voice" not in events_cfg.sources:
        events_cfg = EventsConfig(sources={
            **events_cfg.sources,
            "voice": SourceConfig(name="voice", enabled=True, params={}),
        })
    return _inject_heartbeat(events_cfg, daemon_cfg.heartbeat_sec)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def build_daemon(opts: DaemonOptions) -> DaemonSubsystems:
    """Wire every daemon subsystem and return them as a populated record.

    Boot order matches the original ``daemon/main.py``: store → prefs →
    policy → MCP → checkpointer → sweeper → bus → sources → channels →
    models → skills → search → subagents → triage agent → loops. The
    caller is responsible for installing signal handlers, scheduling the
    loop tasks, and ordered shutdown.
    """
    workspace = opts.workspace.resolve()
    home = state_dir()

    daemon_cfg = DaemonConfig.from_env()
    events_cfg = _build_initial_events_config(opts, daemon_cfg)

    # ── storage backend (sqlite default / postgres) ------------------------
    # Opened before everything that persists: the checkpointer and the
    # context/memory stores dispatch on whether the pool came up. A dead
    # Postgres falls back to SQLite loudly (timeline event after channels
    # exist) unless YUYUTSAVA_STORAGE_REQUIRE=1.
    storage = StorageSettings.from_env()
    pg_pool: PgPool | None = None
    storage_fallback_reason: str | None = None
    if storage.is_postgres():
        try:
            pg_pool = PgPool(storage)
            await pg_pool.open()
            await pg_migrations.apply(pg_pool)
            logger.info("  storage   : postgres")
        except Exception as exc:
            if storage.require:
                logger.error(
                    "storage: postgres unavailable and YUYUTSAVA_STORAGE_REQUIRE=1 "
                    "— refusing to boot"
                )
                raise
            storage_fallback_reason = (
                f"Postgres unavailable ({exc}); using SQLite for this run. "
                "Checkpoints/artifacts written now are INVISIBLE to Postgres."
            )
            logger.error("storage: %s", storage_fallback_reason)
            if pg_pool is not None:
                await pg_pool.close()
            pg_pool = None
            from dataclasses import replace as _dc_replace
            storage = _dc_replace(storage, backend="sqlite")
    else:
        logger.info("  storage   : sqlite (set YUYUTSAVA_STORAGE_BACKEND=postgres for durable mode)")

    # ── context controller stores ------------------------------------------
    if pg_pool is not None:
        artifact_store = PgArtifactStore(pg_pool)
        summary_store = PgThreadSummaryStore(pg_pool)
    else:
        artifact_store = SqliteArtifactStore(state_db_path())
        summary_store = SqliteThreadSummaryStore(state_db_path())

    # ── semantic memory (default-on when postgres is live) -----------------
    mem_settings = MemorySettings.from_env(default_enabled=pg_pool is not None)
    memory_store: MemoryStore | None = None
    embedder: Embedder | None = None
    if mem_settings.enabled:
        if pg_pool is not None:
            embedder = Embedder(mem_settings)
            memory_store = PgMemoryStore(pg_pool, embedder)
            logger.info("  memory    : pgvector (embed=%s)", mem_settings.embed_model)
        else:
            memory_store = SqliteMemoryStore(state_db_path())
            logger.info("  memory    : sqlite keyword fallback (no embeddings)")
    memory_injector = (
        MemoryInjector(memory_store, top_k=mem_settings.top_k)
        if memory_store is not None else None
    )

    # ── store --------------------------------------------------------------
    store = Store()
    await store.start()

    # ── user prefs store --------------------------------------------------
    prefs_store = PrefsStore(store)
    prefs_injector = PrefsInjector(prefs_store)

    # ── permissions policy (Tier-1.5: auto_approve, ws_* daily caps) ------
    policy = PermissionsPolicy.from_file()
    set_default_policy(policy)
    cap_enforcer = StorePolicyCapEnforcer(policy=policy, store=store)
    if policy.entries:
        logger.info("  policy    : %d rule(s) from permissions.json", len(policy.entries))

    # ── MCP servers -------------------------------------------------------
    mcp_manager = MCPClientManager()
    mcp_cfg = MCPConfig.from_file()
    await mcp_manager.start(mcp_cfg)
    if mcp_manager.known_servers():
        logger.info("  mcp       : %s", ", ".join(mcp_manager.known_servers()))

    # ── checkpointer (sqlite or postgres; sweeper handles stale threads) --
    checkpointer_saver = CheckpointerSaver(db_path=checkpoints_db_path(), storage=storage)
    checkpointer = await checkpointer_saver.start()

    # ── unified TTL sweeper (checkpoints + on-disk blobs + event rows) ---
    # Webcam frames pile up fast (potentially one every few seconds for
    # hours). Keep ~1h of history then delete files + matching
    # event_payloads rows. Enrolled-faces DB at ~/.yuyutsava/deepface/ is
    # in a sibling directory and is NEVER swept — that's user data.
    sweeper = UnifiedSweeper(
        store=store,
        checkpoint_saver=checkpointer,
        blob_targets=[
            BlobSweepTarget(
                name="webcam",
                directory=blobs_dir() / "webcam",
                ttl_sec=3600,
                glob="*.jpg",
            ),
        ],
        config=SweeperConfig(),
        artifact_store=artifact_store,
    )

    # ── bus ---------------------------------------------------------------
    bus = EventBus()

    # ── sources -----------------------------------------------------------
    registry = SourceRegistry(bus, store, events_cfg)
    await registry.start_all()

    async def _hot_reload_events_config() -> None:
        """Re-read events_config.json and rebind sources in place."""
        new_cfg = _inject_heartbeat(EventsConfig.from_file(), daemon_cfg.heartbeat_sec)
        await registry.reload(new_cfg)
        logger.info("config reload: events sources now %s",
                    ", ".join(new_cfg.sources.keys()) or "(none)")

    # ── channels ----------------------------------------------------------
    channels = ChannelRouter(channels=[], primary_name="web")
    web_hub: WebHub | None = None
    web_server: uvicorn.Server | None = None

    if not opts.headless:
        web_hub = WebHub(store)
        channels.channels.append(WebChannel(web_hub))

    # Always include terminal as a fallback (and only channel in headless mode).
    channels.channels.append(TerminalChannel(verbose=opts.verbose))
    if opts.headless:
        channels.primary_name = "terminal"

    # Voice channel — optional, disabled by default (privacy).
    if opts.voice:
        try:
            from yuyutsava.daemon.voice_channel import voice_channel_from_env
            vc = voice_channel_from_env()
            channels.channels.append(vc)
            logger.info("  voice     : enabled (TTS=%s STT=%s)",
                        type(vc._tts).__name__, type(vc._stt).__name__)
        except Exception:
            logger.warning("voice channel init failed — running without voice", exc_info=True)

    # Surface any Postgres→SQLite fallback on the user channels — silent
    # divergence (checkpoints landing where Postgres can't see them) is the
    # one failure mode that must never be quiet.
    for reason in (storage_fallback_reason, checkpointer_saver.fallback_reason):
        if reason:
            await channels.post_event(ChannelEvent(
                payload=TimelinePayload(line=f"storage: {reason}", cls="event-error"),
            ))

    # ── models ------------------------------------------------------------
    triage_settings = llm_settings_from_env("triage")
    orchestrator_settings = llm_settings_from_env("orchestrator")
    subagent_settings = llm_settings_from_env("subagent")
    triage_model = chat_model(triage_settings, temperature=0.0)
    orchestrator_model = chat_model(orchestrator_settings, temperature=0.0)
    subagent_model = chat_model(subagent_settings, temperature=0.1)

    # Compaction model: role "compaction" so a cheap/local model can own
    # summarization (COMPACTION_LLM_PROVIDER=ollama …); falls back to the
    # main provider settings when the role is unset.
    compaction_model = chat_model(llm_settings_from_env("compaction"), temperature=0.0)
    context_settings = ContextSettings.from_env(
        "orchestrator",
        provider=_env("LLM_PROVIDER", "orchestrator", "groq"),
    )
    logger.info(
        "  context   : compact >%d tokens, keep %d msgs, offload >%d chars",
        context_settings.compact_trigger_tokens,
        context_settings.keep_messages,
        context_settings.offload_threshold_chars,
    )

    # ── skills registry ---------------------------------------------------
    skill_registry = SkillRegistry(home_dir=home / "skills")
    logger.info("  skills    : %d bundled, scanning personal + workspace",
                len([s for s in skill_registry.scan() if s.scope == "bundled"]))

    # ── search config (ws_* tools) ---------------------------------------
    search_config = SearchConfig.from_env()
    available = search_config.is_available()
    if any(available.values()):
        logger.info("  search    : %s", ", ".join(p for p, ok in available.items() if ok))

    # ── subagents ---------------------------------------------------------
    task_runner = TaskRunnerAgent(workspace_root=workspace, policy=policy)
    subagent_list = [
        FileOrganizerAgent(
            task_runner, store,
            skill_registry=skill_registry,
            mcp_manager=mcp_manager,
            search_config=search_config,
            cap_enforcer=cap_enforcer,
        ),
        FaceWatcherAgent(
            task_runner, store,
            skill_registry=skill_registry,
            mcp_manager=mcp_manager,
            search_config=search_config,
            cap_enforcer=cap_enforcer,
        ),
        # name="general-purpose" suppresses deepagents' built-in default.
        GeneralPurposeAgent(
            task_runner,
            skill_registry=skill_registry,
            mcp_manager=mcp_manager,
            search_config=search_config,
            cap_enforcer=cap_enforcer,
        ),
    ]
    subagents = {sa.name: sa for sa in subagent_list}

    # ── async (background) subagent host + mirror + watcher --------------
    # Gated by env so this is opt-in for v1. To enable:
    #   export YUYUTSAVA_ASYNC_SUBAGENTS=1
    # Subagents are exposed as `<name>-bg` peers alongside their sync entries.
    import os
    async_host = None
    async_host_url: str | None = None
    async_mirror = None
    async_watcher = None
    session_origin = None
    async_host_attachment = None
    if os.environ.get("YUYUTSAVA_ASYNC_SUBAGENTS", "").lower() in ("1", "true", "yes"):
        from yuyutsava.async_subagents.host import AsyncSubagentHost
        from yuyutsava.async_subagents.host_lock import (
            acquire_or_attach_host,
            register_host_cleanup,
        )
        from yuyutsava.async_subagents.mirror import AsyncTaskMirror
        from yuyutsava.async_subagents.session_origin import SessionOriginMap
        from yuyutsava.async_subagents.watcher import AsyncTaskHealthWatcher
        from yuyutsava.daemon.orchestrator_loop import make_ask_handler

        logger.info("  async subs: enabled (YUYUTSAVA_ASYNC_SUBAGENTS=1)")

        # First-come-wins shared host. If another process (typically a CLI
        # chat started before the daemon) already owns the LangGraph dev
        # server, attach to it instead of starting a second one.
        def _build_host() -> AsyncSubagentHost:
            return AsyncSubagentHost.from_subagents(
                subagent_list,
                model=subagent_model,
                checkpointer=checkpointer,
            )

        attachment = await asyncio.to_thread(
            acquire_or_attach_host, factory=_build_host
        )
        async_host_attachment = attachment
        async_host_url = attachment.url
        async_host = attachment.host  # None when attaching to another owner
        register_host_cleanup(attachment)

        if attachment.host is not None:
            logger.info("  async host: %s (owner; graphs=%s)",
                        attachment.url, attachment.host.graph_ids)
        else:
            logger.info("  async host: %s (attached to running owner)", attachment.url)

        async_mirror = AsyncTaskMirror()
        session_origin = SessionOriginMap()
        channels.session_origin = session_origin

        async_watcher = AsyncTaskHealthWatcher(
            mirror=async_mirror,
            host_url=async_host_url,
            ask_handler=make_ask_handler(
                channels,
                default_session_id="bg-orphan",
                default_agent_path="orchestrator",
            ),
            event_sink=channels.post_event,
            agent_path_root="orchestrator",
        )
        await async_watcher.start()
        logger.info("  async watcher: running")
    else:
        logger.info("  async subs: disabled (set YUYUTSAVA_ASYNC_SUBAGENTS=1 to enable)")

    capabilities_block = render_capabilities_block(
        list(subagents.values()),
        async_subagents=subagent_list if async_host_url is not None else None,
    )

    # ── triage agent + loop ----------------------------------------------
    triage = TriageAgent(triage_model)
    task_queue: asyncio.Queue[OrchestratorTask] = asyncio.Queue()

    triage_loop = TriageLoop(
        bus=bus, store=store, channels=channels, triage=triage,
        capabilities_block=capabilities_block,
        task_queue=task_queue,
        proposal_expiry_sec=daemon_cfg.proposal_expiry_sec,
        skill_registry=skill_registry,
    )

    orch_deps = OrchestratorDeps(
        subagents=subagents,
        subagent_model=subagent_model,
        channels=channels,
        store=store,
        subagent_token_budget=daemon_cfg.subagent_token_budget,
        skill_registry=skill_registry,
        workspace_root=workspace,
        mcp_manager=mcp_manager,
        search_config=search_config,
        cap_enforcer=cap_enforcer,
        async_subagents=subagent_list if async_host_url is not None else None,
        async_host_url=async_host_url,
        async_task_mirror=async_mirror,
        artifact_store=artifact_store,
        summary_store=summary_store,
        memory_store=memory_store,
        context_settings=context_settings,
        compaction_model=compaction_model,
    )
    orch_loop = OrchestratorLoop(
        task_queue=task_queue,
        channels=channels,
        store=store,
        orchestrator_model=orchestrator_model,
        deps=orch_deps,
        orchestrator_token_budget=daemon_cfg.orchestrator_token_budget,
        checkpointer=checkpointer,
        prefs_injector=prefs_injector,
        memory_injector=memory_injector,
    )

    # ── web server -------------------------------------------------------
    if web_hub is not None:
        app = make_app(
            web_hub,
            host=daemon_cfg.web_host,
            skill_registry=skill_registry,
            config_reload=_hot_reload_events_config,
            channels=channels,
            session_origin=session_origin,
        )
        uvicorn_level = logging.getLevelName(
            logging.getLogger("yuyutsava").getEffectiveLevel()
        ).lower()
        config = uvicorn.Config(
            app, host=daemon_cfg.web_host, port=daemon_cfg.web_port,
            log_level=uvicorn_level, access_log=True, lifespan="on",
        )
        web_server = uvicorn.Server(config)

    web_url = f"http://{daemon_cfg.web_host}:{daemon_cfg.web_port}/"

    return DaemonSubsystems(
        daemon_cfg=daemon_cfg,
        home=home,
        workspace=workspace,
        store=store,
        prefs_store=prefs_store,
        bus=bus,
        registry=registry,
        channels=channels,
        web_hub=web_hub,
        web_server=web_server,
        web_url=web_url,
        mcp_manager=mcp_manager,
        checkpointer_saver=checkpointer_saver,
        sweeper=sweeper,
        pg_pool=pg_pool,
        artifact_store=artifact_store,
        summary_store=summary_store,
        memory_store=memory_store,
        embedder=embedder,
        task_queue=task_queue,
        triage_loop=triage_loop,
        orch_loop=orch_loop,
        skill_registry=skill_registry,
        triage_settings=triage_settings,
        orchestrator_settings=orchestrator_settings,
        subagent_names=tuple(subagents.keys()),
        async_host=async_host,
        async_task_mirror=async_mirror,
        async_task_watcher=async_watcher,
        session_origin=session_origin,
        async_host_attachment=async_host_attachment,
        hot_reload_events_config=_hot_reload_events_config,
    )
