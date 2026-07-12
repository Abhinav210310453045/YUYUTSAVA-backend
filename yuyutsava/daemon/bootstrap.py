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
import os
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
from yuyutsava.agents.task_runner.tools import set_default_consent, set_default_policy
from yuyutsava.consent import ConsentRegistry
from yuyutsava.agents.triage.agent import TriageAgent
from yuyutsava.async_subagents.launch_index import LaunchIndex
from yuyutsava.async_subagents.session_origin import SessionOriginMap
from yuyutsava.channels.config import ChannelsConfig
from yuyutsava.channels.plugin import InboundSink
from yuyutsava.channels.registry import ChannelPluginRegistry
from yuyutsava.context.artifacts import PgArtifactStore, SqliteArtifactStore
from yuyutsava.context.config import ContextSettings
from yuyutsava.context.injector import MemoryInjector
from yuyutsava.context.summary_store import PgThreadSummaryStore, SqliteThreadSummaryStore
from yuyutsava.context.transcript_store import PgTranscriptStore, SqliteTranscriptStore
from yuyutsava.storage.voice_store import PgVoiceMessageStore, SqliteVoiceMessageStore
from yuyutsava.core.config import (
    DaemonConfig, EventsConfig, LlmSettings, SearchConfig, SourceConfig,
    _env, llm_settings_from_env,
)
from yuyutsava.core.llm import chat_model
from yuyutsava.core.model_router import ComplexityScorer, ModelRouter
from yuyutsava.core.policy import PermissionsPolicy, StorePolicyCapEnforcer
from yuyutsava.daemon.channels import ChannelEvent, ChannelRouter, TimelinePayload
from yuyutsava.daemon.checkpointing import CheckpointerSaver
from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop
from yuyutsava.daemon.resources import AdmissionController, ResourceMonitor, ResourceSettings
from yuyutsava.daemon.task_registry import PgTaskStore, SqliteTaskStore, TaskRegistry
from yuyutsava.daemon.task_submission import TaskSubmissionService
from yuyutsava.daemon.terminal_channel import TerminalChannel
from yuyutsava.daemon.triage_loop import OrchestratorTask, TriageLoop
from yuyutsava.daemon.usage import PgUsageStore, SqliteUsageStore, UsageStore
from yuyutsava.daemon.web.auth import AuthSettings
from yuyutsava.daemon.web.server import WebChannel, WebHub, make_app
from yuyutsava.daemon.web.services.decision_service import DecisionService
from yuyutsava.events.bus import EventBus
from yuyutsava.events.registry import SourceRegistry
from yuyutsava.mcp.config import MCPConfig
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.memory.config import MemorySettings
from yuyutsava.memory.embedder import Embedder
from yuyutsava.memory.store import MemoryStore, PgMemoryStore, SqliteMemoryStore
from yuyutsava.prefs.injector import PrefsInjector
from yuyutsava.skills.injector import SkillInjector
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.skills.store import PgSkillStore, SkillIndexer, SqliteSkillStore
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.events import Store
from yuyutsava.storage.paths import blobs_dir, checkpoints_db_path, state_db_path, state_dir
from yuyutsava.storage.pg import migrations as pg_migrations
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.routing.health import StorageHealth
from yuyutsava.storage.routing.reconcile import CONTENT_TABLE_SPECS, Reconciler
from yuyutsava.storage.prefs import PrefsStore
from yuyutsava.storage.sessions import PgSessionStore, set_default_session_store
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
    storage_health: object | None
    artifact_store: object
    summary_store: object
    transcript_store: object
    memory_store: object | None
    embedder: object | None

    # subagents + queue + loops
    task_queue: asyncio.Queue[OrchestratorTask]
    triage_loop: TriageLoop
    orch_loop: OrchestratorLoop

    # task gateway (Phase 2). Both are borrowed by the web app; the
    # registry's stores ride the same backends torn down above (pg_pool /
    # per-call sqlite connections), so neither needs its own teardown.
    task_registry: TaskRegistry
    task_submission: TaskSubmissionService

    # channel plugins (Phase 3). ``channel_plugins`` owns running plugin
    # instances — main.py calls stop_all() before channels.shutdown().
    decision_service: DecisionService
    channel_plugins: ChannelPluginRegistry

    # model routing + cost tracking (Phase 4). Borrowed by the web app and
    # the orchestrator loop; the usage store rides the same backends torn
    # down above (pg_pool / per-call sqlite), so no teardown of its own.
    usage_store: UsageStore
    model_router: ModelRouter

    # resource governor (Phase 5). The monitor is a loop main.py schedules
    # alongside triage/orchestrator (joins on stop_event — no teardown hook);
    # the admission controller is borrowed by the orchestrator loop + web app.
    resource_monitor: ResourceMonitor
    admission: AdmissionController

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

    # ── embedder (shared by memory, skills, and the artifact recall index) --
    # Built here, before the stores, so PgArtifactStore can opt into the
    # semantic index (Context REPL) when Postgres + an embedder are live. One
    # embedder per process; memory/skills below reuse this same instance.
    mem_settings = MemorySettings.from_env(default_enabled=pg_pool is not None)
    embedder: Embedder | None = Embedder(mem_settings) if pg_pool is not None else None
    _semantic_recall = ContextSettings.from_env().semantic_recall

    # ── context controller stores ------------------------------------------
    if pg_pool is not None:
        artifact_store = PgArtifactStore(
            pg_pool, embedder=embedder, semantic_recall=_semantic_recall
        )
        if artifact_store.supports_recall:
            logger.info("  ctx recall: pgvector artifact index enabled (ctx_recall)")
        summary_store = PgThreadSummaryStore(pg_pool)
        transcript_store = PgTranscriptStore(pg_pool)
        voice_store = PgVoiceMessageStore(pg_pool)
        # Sessions move to Postgres too (migration v6). Inject the shared pool
        # so the web router's get_default_session_store() reuses it; migrations
        # already ran above, so the store skips its own lazy schema-ensure.
        set_default_session_store(PgSessionStore(storage, pool=pg_pool))
        # Visuals + feedback default stores are injected below, after
        # storage_health exists, wrapped in RoutedStore for spillover failover.
    else:
        artifact_store = SqliteArtifactStore(state_db_path())
        summary_store = SqliteThreadSummaryStore(state_db_path())
        transcript_store = SqliteTranscriptStore(state_db_path())
        voice_store = SqliteVoiceMessageStore(state_db_path())

    # ── task registry (Phase 2: first-class task tracking) -----------------
    task_store = (
        PgTaskStore(pg_pool) if pg_pool is not None
        else SqliteTaskStore(state_db_path())
    )
    task_registry = TaskRegistry(task_store)

    # ── usage accounting + model routing (Phase 4) --------------------------
    usage_store: UsageStore = (
        PgUsageStore(pg_pool) if pg_pool is not None
        else SqliteUsageStore(state_db_path())
    )
    model_router = ModelRouter.from_env()
    if model_router.enabled:
        logger.info("  routing   : complexity-based model routing enabled")

    # ── semantic memory (default-on when postgres is live) -----------------
    # mem_settings + embedder were built above (shared with the artifact recall
    # index); memory and skills reuse the same embedder instance.
    memory_store: MemoryStore | None = None
    if mem_settings.enabled:
        if pg_pool is not None and embedder is not None:
            memory_store = PgMemoryStore(
                pg_pool, embedder,
                min_score=mem_settings.min_score,
                dedup_threshold=mem_settings.dedup_threshold,
            )
            logger.info("  memory    : pgvector (embed=%s)", mem_settings.embed_model)
        else:
            memory_store = SqliteMemoryStore(state_db_path())
            logger.info("  memory    : sqlite keyword fallback (no embeddings)")
    memory_injector = (
        MemoryInjector(memory_store, top_k=mem_settings.top_k)
        if memory_store is not None else None
    )

    # ── store (events DB: postgres-primary + sqlite spillover buffer) ------
    # On the Postgres backend each domain becomes a RoutedStore that fails over
    # to a SQLite buffer when Postgres is unreachable; the health probe drains
    # the buffer back on recovery (storage/routing). SQLite mode keeps the
    # SQLite twins as the permanent primary.
    storage_health = StorageHealth(pg_pool) if pg_pool is not None else None
    store = Store.for_backend(storage, pg_pool, storage_health)
    await store.start()

    # ── visuals + feedback default stores (Postgres-primary, spillover) ----
    # These are the REST-path stores written OUTSIDE a checkpointed turn (the
    # 👍/👎 endpoint, the /visuals API), so a Postgres blip here would otherwise
    # lose the write. Wrap the Pg store + SQLite twin in the shared RoutedStore
    # so an outage buffers to SQLite and the Reconciler (below) drains it back on
    # recovery. On the SQLite backend the getters fall back to the twins lazily.
    if pg_pool is not None and storage_health is not None:
        from yuyutsava.storage.feedback_store import (
            PgFeedbackStore, SqliteFeedbackStore, set_default_feedback_store,
        )
        from yuyutsava.storage.routing.facade import RoutedStore
        from yuyutsava.visuals.store import (
            PgVisualStore, SqliteVisualStore, set_default_visual_store,
        )

        set_default_visual_store(RoutedStore(
            PgVisualStore(pg_pool), SqliteVisualStore(state_db_path()),
            storage_health, name="visual",
        ))
        set_default_feedback_store(RoutedStore(
            PgFeedbackStore(pg_pool), SqliteFeedbackStore(state_db_path()),
            storage_health, name="feedback",
        ))
        # TODO board: also a REST-path store (the /todos router + todo_* tools),
        # same spillover treatment; its TableSpecs drain via CONTENT_TABLE_SPECS.
        from yuyutsava.todoboard.store import (
            PgTodoStore, SqliteTodoStore, set_default_todo_store,
        )
        set_default_todo_store(RoutedStore(
            PgTodoStore(pg_pool), SqliteTodoStore(state_db_path()),
            storage_health, name="todo",
        ))

    # ── user prefs store --------------------------------------------------
    prefs_store = PrefsStore(store)
    prefs_injector = PrefsInjector(prefs_store)

    # ── permissions policy (Tier-1.5: auto_approve, ws_* daily caps) ------
    policy = PermissionsPolicy.from_file()
    set_default_policy(policy)
    cap_enforcer = StorePolicyCapEnforcer(policy=policy, store=store)
    if policy.entries:
        logger.info("  policy    : %d rule(s) from permissions.json", len(policy.entries))

    # ── consent / allowlist registry (Tier-2: "allow for session/project") -
    # One DI singleton shared by every tr_* tool call (via set_default_consent)
    # and the explicit TaskRunner gateway below. Persisted PROJECT grants are
    # loaded from state.db; SESSION grants live in memory for the process.
    consent_registry = ConsentRegistry(store=store)
    set_default_consent(consent_registry)

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
            # deepagents scratch under the daemon workspace: the eviction cache
            # (large tool results) and the summarization transcript dumps. The
            # durable copies live in the DB (artifacts + transcript_messages),
            # so these files are disposable. 24h TTL outlives any single task
            # that might still read_file() an evicted result mid-run, while
            # bounding accumulation in the long-lived shared workspace. The CLI
            # deletes the same dirs synchronously via cleanup_local_sandbox.
            BlobSweepTarget(
                name="large_tool_results",
                directory=workspace / "large_tool_results",
                ttl_sec=24 * 3600,
                glob="*",
            ),
            BlobSweepTarget(
                name="conversation_history",
                directory=workspace / "conversation_history",
                ttl_sec=24 * 3600,
                glob="*",
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
        changed = await registry.reload(new_cfg)
        if changed:
            logger.info("config reload: events sources now %s",
                        ", ".join(new_cfg.sources.keys()) or "(none)")

    # ── channels ----------------------------------------------------------
    channels = ChannelRouter(channels=[], primary_name="web")
    # Origin-aware HITL routing is always on (Phase 3): CLI attach and
    # channel plugins both map session ids to their channel. Previously
    # constructed only when async subagents were enabled.
    session_origin = SessionOriginMap()
    channels.session_origin = session_origin
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
                        os.environ.get("TTS_PROVIDER", "piper"), type(vc._stt).__name__)
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

    # ── resource governor (Phase 5) ----------------------------------------
    # Monitor samples psutil into a ring (main.py schedules its run loop);
    # admission gates heavy tasks (complexity ≥ threshold / heavy hints)
    # behind a semaphore + load check in OrchestratorLoop._run_task. The
    # activity probe is assigned after construction because the controller
    # it asks "is anything running?" needs the monitor first.
    res_settings = ResourceSettings.from_env()
    resource_monitor = ResourceMonitor(res_settings, event_sink=channels.post_event)
    admission = AdmissionController(
        resource_monitor, res_settings,
        registry=task_registry, event_sink=channels.post_event,
    )
    resource_monitor.activity_probe = lambda: bool(admission.active())
    logger.info(
        "  resources : cpu<%.0f%% mem>%dMB disk>%.0fGB, heavy=complexity≥%d (max %d)",
        res_settings.cpu_high_pct, res_settings.mem_min_mb,
        res_settings.disk_min_gb, res_settings.heavy_complexity,
        res_settings.max_heavy_tasks,
    )

    # ── models ------------------------------------------------------------
    triage_settings = llm_settings_from_env("triage")
    orchestrator_settings = llm_settings_from_env("orchestrator")
    subagent_settings = llm_settings_from_env("subagent")
    # Triage is a single-shot classifier — reasoning is wasteful here and (on
    # thinking models like gemini-2.5-flash) eats the token budget, truncating
    # the decision JSON. Disable it so the structured output always completes.
    triage_model = chat_model(triage_settings, temperature=0.0, disable_reasoning=True)
    orchestrator_model = chat_model(orchestrator_settings, temperature=0.0)
    subagent_model = chat_model(subagent_settings, temperature=0.1)

    # Warm the Langfuse reachability probe now — before the async host installs
    # blockbuster — so the runtime path never does urllib on the event loop.
    from yuyutsava.core.tracing import warm_reachability_cache
    await asyncio.to_thread(warm_reachability_cache)

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

    # Semantic skill index — shares the memory embedder. pgvector when live,
    # else the SQLite keyword twin. Caught up to on-disk skills at boot so a
    # skill saved in a prior session is retrievable now.
    if pg_pool is not None and embedder is not None:
        skill_store: object = PgSkillStore(pg_pool, embedder, min_score=mem_settings.min_score)
    else:
        skill_store = SqliteSkillStore(state_db_path())
    try:
        await SkillIndexer.sync(skill_registry, skill_store)
    except Exception:
        logger.warning("skills: index sync failed", exc_info=True)
    skill_injector = SkillInjector(
        skill_store, agent="orchestrator", top_k=mem_settings.top_k
    )

    # ── storage spillover recovery -----------------------------------------
    # On Postgres recovery the health probe drains the buffered SQLite rows back
    # into Postgres (drain-and-delete: no duplication) and re-embeds any
    # vector-less memory/skill rows via their backfill(). The degrade notifier
    # surfaces the outage on the user timeline (never silently divergent).
    if storage_health is not None and pg_pool is not None:
        _backfills = []
        for _store_with_vectors in (memory_store, skill_store):
            _bf = getattr(_store_with_vectors, "backfill_embeddings", None)
            if _bf is not None:
                _backfills.append(_bf)
        _reconciler = Reconciler(
            store.sqlite_backend, pg_pool, backfills=_backfills,
            content_specs=CONTENT_TABLE_SPECS,  # visuals + feedback (RoutedStore-wrapped)
        )
        storage_health.set_recover(_reconciler.reconcile)

        def _on_storage_degraded(reason: str) -> None:
            try:
                asyncio.create_task(channels.post_event(ChannelEvent(
                    payload=TimelinePayload(
                        line=f"storage: postgres unreachable — buffering to SQLite ({reason})",
                        cls="event-error",
                    ),
                )))
            except RuntimeError:
                pass

        storage_health.set_degrade(_on_storage_degraded)

    # ── search config (ws_* tools) ---------------------------------------
    search_config = SearchConfig.from_env()
    available = search_config.is_available()
    if any(available.values()):
        logger.info("  search    : %s", ", ".join(p for p, ok in available.items() if ok))

    # ── subagents ---------------------------------------------------------
    task_runner = TaskRunnerAgent(
        workspace_root=workspace, policy=policy, consent=consent_registry,
    )
    subagent_list = [
        FileOrganizerAgent(
            task_runner, store,
            skill_registry=skill_registry,
            can_write_skills=True,
            mcp_manager=mcp_manager,
            search_config=search_config,
            cap_enforcer=cap_enforcer,
            memory_store=memory_store,
            skill_store=skill_store,
        ),
        FaceWatcherAgent(
            task_runner, store,
            skill_registry=skill_registry,
            can_write_skills=True,
            mcp_manager=mcp_manager,
            search_config=search_config,
            cap_enforcer=cap_enforcer,
            memory_store=memory_store,
            skill_store=skill_store,
        ),
        # name="general-purpose" suppresses deepagents' built-in default.
        GeneralPurposeAgent(
            task_runner,
            skill_registry=skill_registry,
            can_write_skills=True,
            mcp_manager=mcp_manager,
            search_config=search_config,
            cap_enforcer=cap_enforcer,
            memory_store=memory_store,
            skill_store=skill_store,
        ),
    ]
    subagents = {sa.name: sa for sa in subagent_list}

    # Orchestrator work queue + the launch index that links a background task
    # back to the conversation that started it. Created before the async block so
    # the watcher's completion sink can enqueue master wake-ups onto the queue.
    task_queue: asyncio.Queue[OrchestratorTask] = asyncio.Queue()
    launch_index = LaunchIndex()

    # ── async (background) subagent host + mirror + watcher --------------
    # Gated by env so this is opt-in for v1. To enable:
    #   export YUYUTSAVA_ASYNC_SUBAGENTS=1
    # Subagents are exposed as `<name>-bg` peers alongside their sync entries.
    # (``os`` is already imported at module scope — a second local import here
    # made ``os`` function-local, breaking its earlier use in the voice block.)
    async_host = None
    async_host_url: str | None = None
    async_mirror = None
    async_watcher = None
    async_host_attachment = None
    if os.environ.get("YUYUTSAVA_ASYNC_SUBAGENTS", "").lower() in ("1", "true", "yes"):
        from yuyutsava.async_subagents.host import (
            AsyncSubagentHost,
            resolve_allow_blocking,
        )
        from yuyutsava.async_subagents.host_lock import (
            acquire_or_attach_host,
            register_host_cleanup,
        )
        from yuyutsava.async_subagents.mirror import AsyncTaskMirror
        from yuyutsava.async_subagents.watcher import AsyncTaskHealthWatcher

        logger.info("  async subs: enabled (YUYUTSAVA_ASYNC_SUBAGENTS=1)")

        # First-come-wins shared host. If another process (typically a CLI
        # chat started before the daemon) already owns the LangGraph dev
        # server, attach to it instead of starting a second one.
        # Permissive (warn-only) by default. Strict mode (blockbuster) is NOT yet
        # viable as a default: the events Store (yuyutsava/storage/events/store.py)
        # is a synchronous sqlite3 store driven on the event loop (writer loop +
        # consent/prefs reads), and the task_runner path canonicalization
        # (agents/task_runner/zones.py: os.path.realpath → os.readlink) runs inside
        # subagent tools — both raise under blockbuster. Set YUYUTSAVA_ALLOW_BLOCKING=0
        # to opt into strict mode (e.g. to hunt remaining blocking calls in dev);
        # the bootstrap/web/resource paths are already wrapped for that.
        allow_blocking = resolve_allow_blocking(default=True)

        def _build_host() -> AsyncSubagentHost:
            return AsyncSubagentHost.from_subagents(
                subagent_list,
                model=subagent_model,
                checkpointer=checkpointer,
                allow_blocking=allow_blocking,
            )

        # NOTE: once the host owner starts, it installs blockbuster (unless
        # allow_blocking). From here on, any synchronous blocking I/O run on this
        # event loop (os.replace/mkdir/scandir/socket/sqlite) raises BlockingError
        # — wrap such calls in asyncio.to_thread.
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

        async def _wake_master_on_completion(
            task: "MirroredTask", ok: bool, summary: str
        ) -> None:
            """Re-enter the orchestrator queue when a bg subagent finishes.

            Resolves the launching conversation (so the master continues that
            thread and the reply routes to the right surface) and enqueues a
            ``subagent_completed`` wake-up. When no parent is known we leave the
            task un-notified so ``mirror.render_block`` surfaces it on the next
            organic turn instead.
            """
            rec = launch_index.get(task.task_id)
            parent = task.parent_thread_id or (rec.parent_thread_id if rec else None)
            origin = task.origin or (rec.origin if rec else None) or ""
            if not parent:
                return
            await task_queue.put(OrchestratorTask(
                proposal_id="", event_id="", topic="subagent_completion",
                summary=f"{task.agent_name} {'ok' if ok else 'failed'}",
                instruction="", subagent_hint="", urgency=2,
                kind="subagent_completed",
                origin=origin,
                parent_thread_id=parent,
                completion={
                    "task_id": task.task_id,
                    "agent_name": task.agent_name,
                    "ok": ok,
                    "summary": summary,
                },
            ))
            await async_mirror.mark_notified(task.task_id)

        async_watcher = AsyncTaskHealthWatcher(
            mirror=async_mirror,
            host_url=async_host_url,
            # The watcher builds a fully-formatted AskPrompt (clean title/body via
            # the shared interrupt formatter) and hands it straight to the channel
            # router. (make_ask_handler is for the orchestrator's streaming loop,
            # where the interrupt arrives as a raw dict — passing an AskPrompt to
            # it would stringify the object into the body.)
            ask_handler=channels.post_ask,
            event_sink=channels.post_event,
            agent_path_root="orchestrator",
            completion_sink=_wake_master_on_completion,
            launch_index=launch_index,
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
    # task_queue + launch_index created above (before the async block).

    # ── task submission (POST /tasks; channel plugins in Phase 3) ---------
    # Direct submissions skip triage, so a light-tier call scores their
    # complexity — only when routing is on (a score nobody routes on is
    # wasted spend). The scorer resolves its model lazily and never raises.
    complexity_scorer = (
        ComplexityScorer(lambda: model_router.tier_model("light"))
        if model_router.enabled else None
    )
    task_submission = TaskSubmissionService(
        registry=task_registry,
        task_queue=task_queue,
        store=store,
        bus=bus,
        proposal_expiry_sec=daemon_cfg.proposal_expiry_sec,
        complexity_scorer=complexity_scorer,
    )

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
        transcript_store=transcript_store,
        context_settings=context_settings,
        compaction_model=compaction_model,
        usage_store=usage_store,
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
        skill_injector=skill_injector,
        skill_store=skill_store,
        task_registry=task_registry,
        model_router=model_router,
        admission=admission,
        launch_index=launch_index,
    )

    # ── channel plugins (Phase 3) ------------------------------------------
    # One DecisionService resolves proposal/ask responses for every surface:
    # the HTTP routers and any plugin's inbound loop land on the same code
    # path. Waiter maps are registered per surface (WebHub + InboundSink).
    decision_service = DecisionService(store)
    if web_hub is not None:
        decision_service.add_waiters(
            proposals=web_hub.pending_proposals, asks=web_hub.pending_asks,
        )

    def _daemon_status() -> str:
        return (
            f"daemon: running — web http://{daemon_cfg.web_host}:"
            f"{daemon_cfg.web_port}/ · subagents: {', '.join(subagents)}"
        )

    inbound_sink = InboundSink(
        task_submission=task_submission,
        decision_service=decision_service,
        task_registry=task_registry,
        prefs_store=prefs_store,
        status_provider=_daemon_status,
    )
    decision_service.add_waiters(
        proposals=inbound_sink.pending_proposals, asks=inbound_sink.pending_asks,
    )
    channel_plugins = ChannelPluginRegistry(
        router=channels, sink=inbound_sink, config=ChannelsConfig.from_file(),
    )
    await channel_plugins.start_all()

    # ── web server -------------------------------------------------------
    if web_hub is not None:
        # from_env may mkdir + write the api_token file on a non-loopback bind →
        # off-loop (this runs after the subagent host activates blockbuster).
        auth_settings = await asyncio.to_thread(
            AuthSettings.from_env, host=daemon_cfg.web_host
        )
        if auth_settings.enforce:
            logger.info("  web auth  : bearer token enforced (non-loopback bind %s)",
                        daemon_cfg.web_host)
        # Lazy host for interactive text/voice conversations (Electron/mobile).
        # Builds its shared deepagent bundle on first /ws/converse use and
        # attaches to this daemon's async-subagent host (no second host).
        from yuyutsava.daemon.conversation_manager import ConversationManager
        conversation_manager = ConversationManager(
            workspace=workspace,
            checkpointer=checkpointer,
            settings=orchestrator_settings,
            search_config=search_config,
            voice_store=voice_store,
            usage_store=usage_store,
        )

        app = make_app(
            web_hub,
            host=daemon_cfg.web_host,
            skill_registry=skill_registry,
            config_reload=_hot_reload_events_config,
            channels=channels,
            session_origin=session_origin,
            auth=auth_settings,
            task_registry=task_registry,
            task_submission=task_submission,
            decision_service=decision_service,
            channel_plugins=channel_plugins,
            usage_store=usage_store,
            resource_monitor=resource_monitor,
            admission_controller=admission,
            model_router=model_router,
            memory_store=memory_store,
            conversation_manager=conversation_manager,
            voice_store=voice_store,
            transcript_store=transcript_store,
            async_subagents=async_host_url is not None,
            async_task_watcher=async_watcher,
        )
        uvicorn_level = logging.getLevelName(
            logging.getLogger("yuyutsava").getEffectiveLevel()
        ).lower()
        config = uvicorn.Config(
            app, host=daemon_cfg.web_host, port=daemon_cfg.web_port,
            log_level=uvicorn_level, lifespan="on",
            # uvicorn's access log records full request lines including the
            # query string — which carries ?token= on /stream when auth is
            # enforced. Drop it off-loopback; the in-app HTTP log middleware
            # (path-only) still covers observability.
            access_log=not auth_settings.enforce,
        )
        web_server = uvicorn.Server(config)

    # A wildcard bind (0.0.0.0 / ::) is not an openable address; show loopback
    # for the local web window. Remote clients use the host's tailnet IP.
    _display_host = daemon_cfg.web_host
    if _display_host in ("0.0.0.0", "::", ""):
        _display_host = "127.0.0.1"
    web_url = f"http://{_display_host}:{daemon_cfg.web_port}/"

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
        storage_health=storage_health,
        artifact_store=artifact_store,
        summary_store=summary_store,
        transcript_store=transcript_store,
        memory_store=memory_store,
        embedder=embedder,
        task_queue=task_queue,
        triage_loop=triage_loop,
        orch_loop=orch_loop,
        task_registry=task_registry,
        task_submission=task_submission,
        decision_service=decision_service,
        channel_plugins=channel_plugins,
        usage_store=usage_store,
        model_router=model_router,
        resource_monitor=resource_monitor,
        admission=admission,
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
