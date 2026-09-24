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
import re
from dataclasses import dataclass
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import Any, Awaitable, Callable

import uvicorn

from yuyutsava.agents.face_watcher.agent import FaceWatcherAgent
from yuyutsava.agents.file_organizer.agent import FileOrganizerAgent
from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
from yuyutsava.agents.orchestrator.agent import OrchestratorDeps
from yuyutsava.agents.orchestrator.capabilities import render_capabilities_block
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.task_runner.tools import set_default_consent, set_default_policy
from yuyutsava.agents.tinker.subagent import make_tinker_subagent
from yuyutsava.agents.triage.agent import TriageAgent
from yuyutsava.async_subagents.host import AsyncSubagentHost, resolve_allow_blocking
from yuyutsava.async_subagents.host_lock import acquire_or_attach_host, register_host_cleanup
from yuyutsava.async_subagents.launch_index import LaunchIndex
from yuyutsava.async_subagents.mirror import AsyncTaskMirror, MirroredTask
from yuyutsava.async_subagents.session_origin import SessionOriginMap
from yuyutsava.async_subagents.watcher import AsyncTaskHealthWatcher
from yuyutsava.channels.config import ChannelsConfig
from yuyutsava.channels.plugin import InboundSink
from yuyutsava.channels.registry import ChannelPluginRegistry
from yuyutsava.config.env import env, env_flag
from yuyutsava.config.search import SearchConfig
from yuyutsava.consent import ConsentRegistry
from yuyutsava.context.config import ContextSettings
from yuyutsava.core.async_host import BackgroundStores, build_async_host
from yuyutsava.core.model_router import ComplexityScorer, ModelRouter
from yuyutsava.core.stores import StoreFactory
from yuyutsava.daemon.ask_registry import AskRegistry
from yuyutsava.daemon.ask_resume import AskResumeService
from yuyutsava.daemon.channels import ChannelRouter
from yuyutsava.daemon.checkpointing import CheckpointerSaver
from yuyutsava.daemon.config import DaemonConfig
from yuyutsava.daemon.conversation_manager import ConversationManager
from yuyutsava.daemon.log_bridge import LogBridge
from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop
from yuyutsava.daemon.resources import AdmissionController, ResourceMonitor, ResourceSettings
from yuyutsava.daemon.task_submission import TaskSubmissionService
from yuyutsava.daemon.terminal_channel import TerminalChannel
from yuyutsava.daemon.triage_loop import OrchestratorTask, TriageLoop
from yuyutsava.daemon.voice_channel import voice_channel_from_env
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.app_deps import AppDeps
from yuyutsava.daemon.web.auth import AuthSettings
from yuyutsava.daemon.web.services.decision_service import DecisionService
from yuyutsava.daemon.web.services.stream_service import WebChannel, WebHub
from yuyutsava.events.bus import EventBus
from yuyutsava.events.config import EventsConfig, SourceConfig
from yuyutsava.events.registry import SourceRegistry
from yuyutsava.llm.factory import chat_model
from yuyutsava.llm.settings import LlmSettings, llm_settings_from_env
from yuyutsava.llm.tracing import warm_reachability_cache
from yuyutsava.mcp.config import MCPConfig
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.memory.config import MemorySettings
from yuyutsava.memory.embedder import Embedder
from yuyutsava.memory.store import MemoryStore
from yuyutsava.policy.builtin.tool_permissions import PermissionsPolicy, StorePolicyCapEnforcer
from yuyutsava.ports import CapEnforcer
from yuyutsava.ports.channels import ChannelEvent, TimelinePayload
from yuyutsava.prefs.injector import PrefsInjector
from yuyutsava.prefs.runtime import RuntimeSettings
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.skills.store import SkillIndexer
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.events import Store
from yuyutsava.storage.feedback_store import set_default_feedback_store
from yuyutsava.storage.paths import (
    WorkspaceLayout,
    blobs_dir,
    checkpoints_db_path,
    state_dir,
)
from yuyutsava.storage.pg import migrations as pg_migrations
from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.prefs import PrefsStore
from yuyutsava.storage.routing.health import StorageHealth
from yuyutsava.storage.routing.reconcile import CONTENT_TABLE_SPECS, Reconciler
from yuyutsava.storage.sessions import PgSessionStore, set_default_session_store
from yuyutsava.storage.sweeper import BlobSweepTarget, SweeperConfig, UnifiedSweeper
from yuyutsava.storage.task_registry import TaskRegistry
from yuyutsava.storage.usage import UsageStore
from yuyutsava.todoboard.exchange import get_default_exchange
from yuyutsava.todoboard.recall import TodoNoteIndex, set_default_note_index
from yuyutsava.todoboard.store import set_default_todo_store
from yuyutsava.visuals.store import set_default_visual_store

logger = logging.getLogger("yuyutsava.daemon.bootstrap")

# Trailer a background subagent appends to its summary listing showable
# artifact ids (see the tinker subagent prompt): "ARTIFACTS: id1, id2".
_ARTIFACTS_TRAILER = re.compile(r"(?im)^\s*ARTIFACTS:\s*(.+?)\s*$")


def _parse_artifact_ids(summary: str) -> list[str]:
    """Extract artifact ids from a subagent summary's ARTIFACTS trailer.

    Returns them in order, de-duplicated; empty when the trailer is absent.
    Tolerant of comma/space separators and surrounding punctuation.
    """
    if not summary:
        return []
    ids: list[str] = []
    for m in _ARTIFACTS_TRAILER.finditer(summary):
        for tok in re.split(r"[,\s]+", m.group(1)):
            tok = tok.strip().strip(".,;")
            if tok and tok not in ids:
                ids.append(tok)
    return ids


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
    headless: bool  # --no-ui
    voice: bool  # --voice
    verbose: bool  # --verbose


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
    # Hot toggles (voice mode, subagent deny-list) shared by the web API, the
    # wake bridge, the conversation manager and the agent middleware.
    runtime_settings: RuntimeSettings

    # bus + sources
    bus: EventBus
    registry: SourceRegistry

    # channels + web
    channels: ChannelRouter
    web_hub: WebHub | None
    #: Forwards the daemon's own log records to the UI's Logs panel. None in
    #: headless mode, where there is no web hub to broadcast on.
    log_bridge: Any | None
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

    # task gateway. Both are borrowed by the web app; the
    # registry's stores ride the same backends torn down above (pg_pool /
    # per-call sqlite connections), so neither needs its own teardown.
    task_registry: TaskRegistry
    task_submission: TaskSubmissionService

    # channel plugins. ``channel_plugins`` owns running plugin
    # instances — main.py calls stop_all() before channels.shutdown().
    decision_service: DecisionService
    channel_plugins: ChannelPluginRegistry

    # model routing + cost tracking. Borrowed by the web app and
    # the orchestrator loop; the usage store rides the same backends torn
    # down above (pg_pool / per-call sqlite), so no teardown of its own.
    usage_store: UsageStore
    model_router: ModelRouter

    # resource governor. The monitor is a loop main.py schedules
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
    return EventsConfig(
        sources={
            name: SourceConfig(
                name=src.name,
                enabled=src.enabled,
                params={**src.params, "heartbeat_sec": heartbeat_sec},
            )
            for name, src in events_cfg.sources.items()
        }
    )


def _apply_wake_toggle(events_cfg: EventsConfig, wake_enabled: bool) -> EventsConfig:
    """Force the ``voice`` source's enabled bit to match the runtime toggle.

    The runtime pref is the single owner of wake-word detection: whatever
    ``events_config.json`` (or ``--voice``) says, a user who turned voice mode
    off last session must not have the mic re-opened on the next boot. No-op
    when the source isn't configured at all.
    """
    src = events_cfg.sources.get("voice")
    if src is None or src.enabled == wake_enabled:
        return events_cfg
    logger.info("  voice src : forced enabled=%s by the runtime toggle", wake_enabled)
    return EventsConfig(
        sources={
            **events_cfg.sources,
            "voice": SourceConfig(name="voice", enabled=wake_enabled, params=dict(src.params)),
        }
    )


def _build_initial_events_config(opts: DaemonOptions, daemon_cfg: DaemonConfig) -> EventsConfig:
    """Load events_config.json, fold in --voice, then inject heartbeat."""
    events_cfg = EventsConfig.from_file()
    if opts.voice and "voice" not in events_cfg.sources:
        events_cfg = EventsConfig(
            sources={
                **events_cfg.sources,
                "voice": SourceConfig(name="voice", enabled=True, params={}),
            }
        )
    return _inject_heartbeat(events_cfg, daemon_cfg.heartbeat_sec)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Subsystem builders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageSubsystem:
    """Everything the persistence layer produces, as one explicit record.

    ADR-003. How the daemon fits together is stated by records like this one,
    not by *statement order inside one body*, which no tool can check. It has
    explicit inputs (options) and explicit outputs (this record), so it can be
    built and inspected without constructing a daemon, and the load order it
    depends on is visible in a signature rather than implied by position.
    """

    settings: StorageSettings
    pg_pool: PgPool | None
    storage_health: object | None
    stores: StoreFactory
    embedder: Embedder | None
    mem_settings: object

    # domain stores
    artifact_store: object
    summary_store: object
    transcript_store: object
    voice_store: object
    memory_store: MemoryStore | None
    usage_store: UsageStore
    task_registry: TaskRegistry
    events: Store
    model_router: object

    #: Set when Postgres was requested but unreachable; surfaced on the user
    #: timeline once channels exist, so a silent downgrade is impossible.
    fallback_reason: str | None = None


async def _open_backend() -> tuple[StorageSettings, PgPool | None, str | None]:
    """The storage settings in force, the Postgres pool if it came up, and why not.

    Opened before everything that persists: the checkpointer and the
    context/memory stores dispatch on whether the pool came up. A dead Postgres
    falls back to SQLite loudly (a timeline event once channels exist) unless
    ``YUYUTSAVA_STORAGE_REQUIRE=1``, which refuses to boot instead.
    """
    storage = StorageSettings.from_env()
    if not storage.is_postgres():
        logger.info(
            "  storage   : sqlite (set YUYUTSAVA_STORAGE_BACKEND=postgres for durable mode)"
        )
        return storage, None, None
    pg_pool: PgPool | None = None
    try:
        pg_pool = PgPool(storage)
        await pg_pool.open()
        await pg_migrations.apply(pg_pool)
        logger.info("  storage   : postgres")
        return storage, pg_pool, None
    except Exception as exc:
        if storage.require:
            logger.error(
                "storage: postgres unavailable and YUYUTSAVA_STORAGE_REQUIRE=1 — refusing to boot"
            )
            raise
        reason = (
            f"Postgres unavailable ({exc}); using SQLite for this run. "
            "Checkpoints/artifacts written now are INVISIBLE to Postgres."
        )
        logger.error("storage: %s", reason)
        if pg_pool is not None:
            await pg_pool.close()
        return _dc_replace(storage, backend="sqlite"), None, reason


def _install_rest_stores(stores: StoreFactory, has_spillover: bool) -> None:
    """Point the visual, feedback and todo default stores at Postgres-with-spillover.

    These are the REST-path stores written OUTSIDE a checkpointed turn (the
    👍/👎 endpoint, the /visuals API, the board), so a raised error would lose
    the write outright. Spillover is a declared policy (storage/domains.py:
    Failover): the factory wraps exactly the domains that ask for it, an outage
    buffers to SQLite, and the Reconciler drains it back on recovery. On the
    SQLite backend the getters fall back to the twins lazily.
    """
    if not has_spillover:
        return
    set_default_visual_store(stores.visuals())
    set_default_feedback_store(stores.feedback())
    set_default_todo_store(stores.todos())


async def build_storage(opts: DaemonOptions) -> StorageSubsystem:
    """Open the backend, run migrations, and construct every domain store."""
    storage, pg_pool, storage_fallback_reason = await _open_backend()

    # ── embedder (shared by memory, skills, and the artifact recall index) --
    # Built before the stores, so the Postgres artifact store can opt into the
    # semantic index when Postgres + an embedder are live. One embedder per
    # process; memory and skills reuse this same instance.
    mem_settings = MemorySettings.from_env(default_enabled=pg_pool is not None)
    embedder: Embedder | None = Embedder(mem_settings) if pg_pool is not None else None
    _semantic_recall = ContextSettings.from_env().semantic_recall

    # ── stores -------------------------------------------------------------
    # One StoreFactory resolves "Postgres or SQLite" ONCE (ADR-002), with
    # failover declared per domain in storage/domains.py.
    storage_health = StorageHealth(pg_pool) if pg_pool is not None else None
    stores = StoreFactory(
        storage,
        pg_pool=pg_pool,
        health=storage_health,
        embedder=embedder,
    )

    artifact_store = stores.artifacts(semantic_recall=_semantic_recall)
    summary_store = stores.summaries()
    transcript_store = stores.transcripts()
    voice_store = stores.voice()
    if pg_pool is not None:
        # Sessions live in Postgres too (migration v6). Inject the shared pool
        # so the web router's get_default_session_store() reuses it; migrations
        # already ran, so the store skips its own lazy schema-ensure.
        set_default_session_store(PgSessionStore(storage, pool=pg_pool))

    task_registry = TaskRegistry(stores.tasks())
    usage_store: UsageStore = stores.usage()
    model_router = ModelRouter.from_env()
    if model_router.enabled:
        logger.info("  routing   : complexity-based model routing enabled")
    # Semantic memory, default-on when Postgres is live. The orchestrator
    # recalls it per turn (RetrievalInjectionPolicy), not at build time.
    memory_store: MemoryStore | None = stores.memory(mem_settings)

    # ── events DB: Postgres-primary + SQLite spillover buffer --------------
    # On the Postgres backend each domain is a RoutedStore that fails over to a
    # SQLite buffer when Postgres is unreachable; the health probe drains the
    # buffer back on recovery (storage/routing). SQLite mode keeps the SQLite
    # twins as the permanent primary.
    store = stores.events()
    await store.start()

    _install_rest_stores(stores, pg_pool is not None and storage_health is not None)

    return StorageSubsystem(
        settings=storage,
        pg_pool=pg_pool,
        storage_health=storage_health,
        stores=stores,
        embedder=embedder,
        mem_settings=mem_settings,
        artifact_store=artifact_store,
        summary_store=summary_store,
        transcript_store=transcript_store,
        voice_store=voice_store,
        memory_store=memory_store,
        usage_store=usage_store,
        task_registry=task_registry,
        events=store,
        model_router=model_router,
        fallback_reason=storage_fallback_reason,
    )


@dataclass(frozen=True)
class PolicySubsystem:
    """User preferences, permission rules and consent grants.

    Cohesive because all three read from the
    same events store and all three answer one question: *what is this agent
    allowed to do?* Extracted together rather than split, because separating
    them would produce three builders that each take ``store`` and are never
    used apart.
    """

    prefs_store: PrefsStore
    prefs_injector: PrefsInjector
    runtime_settings: RuntimeSettings
    policy: PermissionsPolicy
    cap_enforcer: CapEnforcer
    consent_registry: ConsentRegistry


async def build_policy(store: Store) -> PolicySubsystem:
    """Load preferences, permission rules and consent grants.

    NOTE: still calls ``set_default_policy`` / ``set_default_consent``, which
    install process-global singletons (finding ``F-S08``, ``R-GLB-01``). They
    are confined to this one named function until injection replaces them.
    """
    # ── user prefs store --------------------------------------------------
    prefs_store = PrefsStore(store)
    prefs_injector = PrefsInjector(prefs_store)
    # Hot runtime toggles (voice mode, dedicated-subagent deny-list). Primed
    # here so every synchronous reader — the converse turn loop, the subagent
    # gate middleware — starts warm instead of racing a first DB read.
    runtime_settings = await RuntimeSettings(prefs_store).load()
    _voice_toggles = runtime_settings.voice()
    if not (_voice_toggles.wake_enabled and _voice_toggles.tts_enabled):
        logger.info(
            "  voice mode: wake=%s tts=%s (runtime toggle)",
            _voice_toggles.wake_enabled,
            _voice_toggles.tts_enabled,
        )

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

    return PolicySubsystem(
        prefs_store=prefs_store,
        prefs_injector=prefs_injector,
        runtime_settings=runtime_settings,
        policy=policy,
        cap_enforcer=cap_enforcer,
        consent_registry=consent_registry,
    )


@dataclass(frozen=True)
class RetentionSubsystem:
    """Checkpointing and the TTL sweeper — what keeps disk bounded.

    The checkpointer and sweeper belong together:
    the sweeper needs the saver to age out stale threads, and both answer
    "what gets kept, and for how long?".
    """

    checkpointer_saver: CheckpointerSaver
    checkpointer: object
    sweeper: UnifiedSweeper


async def build_retention(
    *,
    workspace: Path,
    storage: StorageSettings,
    store: Store,
    artifact_store: object,
    storage_health: object | None,
) -> RetentionSubsystem:
    """Start the checkpointer and assemble the unified TTL sweeper.

    Inputs are explicit rather than read from enclosing scope, so the sweeper's
    dependency on the artifact store and the health handle is in the signature.
    """
    # ── checkpointer (sqlite or postgres; sweeper handles stale threads) --
    checkpointer_saver = CheckpointerSaver(db_path=checkpoints_db_path(), storage=storage)
    checkpointer = await checkpointer_saver.start()

    # ── unified TTL sweeper (checkpoints + on-disk blobs + event rows) ---
    # Webcam frames pile up fast (potentially one every few seconds for
    # hours). Keep ~1h of history then delete files + matching
    # event_payloads rows. Only registered blob dirs are swept; anything an
    # MCP server keeps (e.g. enrolled identities) lives elsewhere — user data.
    layout = WorkspaceLayout.for_workspace(workspace)
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
            # deepagents scratch under <ws>/.yuyutsava/tmp: the eviction cache
            # (large tool results) and the summarization transcript dumps. The
            # durable copies live in the DB (artifacts + transcript_messages),
            # so these files are disposable. 24h TTL outlives any single task
            # that might still read_file() an evicted result mid-run, while
            # bounding accumulation in the long-lived shared workspace. The CLI
            # deletes the same dirs synchronously via cleanup_local_sandbox.
            BlobSweepTarget(
                name="large_tool_results",
                directory=layout.large_tool_results,
                ttl_sec=24 * 3600,
                glob="*",
            ),
            BlobSweepTarget(
                name="conversation_history",
                directory=layout.conversation_history,
                ttl_sec=24 * 3600,
                glob="*",
            ),
        ],
        config=SweeperConfig(),
        artifact_store=artifact_store,
        # Orphaned TODO workspaces (dir without a card row) — card ids come
        # through the exchange; the health handle makes the sweep skip while
        # degraded (the SQLite buffer's card list is partial).
        todo_exchange=get_default_exchange(),
        storage_health=storage_health,
    )

    return RetentionSubsystem(
        checkpointer_saver=checkpointer_saver,
        checkpointer=checkpointer,
        sweeper=sweeper,
    )


@dataclass(frozen=True)
class RetrievalSubsystem:
    """Semantic recall: the skills index and the TODO-board note index.

    The note index is built inside
    ``if pg_pool is not None and embedder is not None``, a **Postgres-only**
    branch — the kind where finding Y's ``NameError`` hid. As its own builder
    it is directly testable on a live Postgres, which ``build_daemon`` (it
    starts servers and never returns) is not.
    """

    skill_registry: SkillRegistry
    skill_store: object
    #: ``TodoNoteIndex`` on Postgres+embedder; ``None`` otherwise, which leaves
    #: the exchange's embed-on-write hooks no-oping.
    note_index: object | None


async def build_retrieval(
    *,
    home: Path,
    workspace: Path,
    stores: StoreFactory,
    mem_settings: object,
    pg_pool: PgPool | None,
    embedder: Embedder | None,
) -> RetrievalSubsystem:
    """Build the skills index and (on Postgres) the TODO-note recall index.

    Both boot-sync against existing on-disk/DB content so a skill or note
    written in a prior session is retrievable now. Sync failures are logged and
    swallowed — a cold index degrades recall, it does not stop the daemon.
    """
    note_index: object | None = None
    # ── skills registry ---------------------------------------------------
    # Workspace scope = <ws>/.yuyutsava/skills; without it the registry fell
    # back to the process cwd and the daemon never scanned workspace skills.
    skill_registry = SkillRegistry(
        home_dir=home / "skills",
        workspace_dir=WorkspaceLayout.for_workspace(workspace).skills,
    )
    logger.info(
        "  skills    : %d bundled, scanning personal + workspace",
        len([s for s in skill_registry.scan() if s.scope == "bundled"]),
    )

    # Semantic skill index — shares the memory embedder. pgvector when live,
    # else the SQLite keyword twin. Caught up to on-disk skills at boot so a
    # skill saved in a prior session is retrievable now.
    skill_store: object = stores.skills(mem_settings)
    try:
        await SkillIndexer.sync(skill_registry, skill_store)
    except Exception:
        logger.warning("skills: index sync failed", exc_info=True)
    # (Skill recall for the orchestrator master likewise rides the per-turn
    # middleware inside build_orchestrator, scoped agent="orchestrator".)

    # ── TODO-board note recall (pgvector, migration v16) --------------------
    # Embed-on-write hooks in the exchange resolve this default index; the boot
    # sync backfills notes written before the feature (or while degraded — a
    # spillover-drained note has no chunks until this catches it). SQLite-only
    # deployments leave the singleton unset and every hook no-ops.
    if pg_pool is not None and embedder is not None:
        note_index = TodoNoteIndex(pg_pool, embedder=embedder, min_score=mem_settings.min_score)
        set_default_note_index(note_index)
        try:
            await note_index.sync(get_default_exchange())
            logger.info("  todo notes: pgvector recall enabled (todo_recall)")
        except Exception:
            logger.warning("todo notes: boot index sync failed", exc_info=True)

    return RetrievalSubsystem(
        skill_registry=skill_registry,
        skill_store=skill_store,
        note_index=note_index,
    )


@dataclass(frozen=True)
class EventsSubsystem:
    """The event bus, its live sources, and the config hot-reload hook.

    ``reload`` is returned as a callable rather than left as a closure in
    ``build_daemon``'s body: the SIGHUP handler needs it, and a returned function
    is visible in a signature where a buried closure is not.
    """

    bus: EventBus
    registry: SourceRegistry
    events_cfg: EventsConfig
    #: Re-reads events_config.json and rebinds sources in place.
    reload: Callable[[], Awaitable[None]]


async def build_events(
    *,
    events_cfg: EventsConfig,
    daemon_cfg: DaemonConfig,
    store: Store,
    runtime_settings: RuntimeSettings,
) -> EventsSubsystem:
    """Start the event bus and every configured source.

    NOTE: this genuinely starts things — webcam capture, filesystem watchers,
    the voice pipeline — so unlike the earlier slices it is not free to call in
    a test. It is extracted for the same reason regardless: the SIGHUP reload
    path and the wake-toggle override were both invisible inside the monolith.
    """
    # ── bus ---------------------------------------------------------------
    bus = EventBus()

    # ── sources -----------------------------------------------------------
    # The persisted voice toggle overrides the on-disk source config so a
    # daemon restart can't silently re-open the mic (see _apply_wake_toggle).
    events_cfg = _apply_wake_toggle(events_cfg, runtime_settings.voice().wake_enabled)
    registry = SourceRegistry(bus, store, events_cfg)
    await registry.start_all()

    async def _hot_reload_events_config() -> None:
        """Re-read events_config.json and rebind sources in place."""
        new_cfg = _inject_heartbeat(EventsConfig.from_file(), daemon_cfg.heartbeat_sec)
        new_cfg = _apply_wake_toggle(new_cfg, runtime_settings.voice().wake_enabled)
        changed = await registry.reload(new_cfg)
        if changed:
            logger.info(
                "config reload: events sources now %s",
                ", ".join(new_cfg.sources.keys()) or "(none)",
            )

    return EventsSubsystem(
        bus=bus,
        registry=registry,
        events_cfg=events_cfg,
        reload=_hot_reload_events_config,
    )


def wire_spillover_recovery(
    storage_health: Any | None,
    pg_pool: Any | None,
    store: Any,
    channels: ChannelRouter,
    *,
    memory_store: Any | None,
    skill_store: Any | None,
) -> None:
    """Teach the health probe how to recover from a Postgres outage, and how to
    say so while it lasts.

    On recovery the buffered SQLite rows are drained back into Postgres
    (drain-and-delete, so nothing is duplicated) and any vector-less memory or
    skill rows are re-embedded. While it lasts, the outage is announced on the
    user timeline: storage quietly diverging — checkpoints landing where
    Postgres cannot see them — is the one failure mode that must never be quiet.
    """
    if storage_health is None or pg_pool is None:
        return
    # Both stores declare backfill_embeddings unconditionally since ADR-002
    # step 2.5b (it returns 0 without vectors), so no getattr probe.
    # `memory_store` can still be None when memory is switched off.
    backfills = [s.backfill_embeddings for s in (memory_store, skill_store) if s is not None]
    reconciler = Reconciler(
        store.sqlite_backend,
        pg_pool,
        backfills=backfills,
        content_specs=CONTENT_TABLE_SPECS,  # visuals + feedback (RoutedStore-wrapped)
    )
    storage_health.set_recover(reconciler.reconcile)

    def _on_storage_degraded(reason: str) -> None:
        try:
            asyncio.create_task(
                channels.post_event(
                    ChannelEvent(
                        payload=TimelinePayload(
                            line=f"storage: postgres unreachable — buffering to SQLite ({reason})",
                            cls="event-error",
                        ),
                    )
                )
            )
        except RuntimeError:
            pass

    storage_health.set_degrade(_on_storage_degraded)


async def build_mcp(workspace: Path) -> MCPClientManager:
    """Start the MCP servers: the global config, plus the workspace's own when
    the workspace is trusted."""
    manager = MCPClientManager()
    cfg = MCPConfig.load(workspace)
    await manager.start(cfg)
    if manager.known_servers():
        ws_cfg = str(WorkspaceLayout.for_workspace(workspace).mcp_config)
        logger.info(
            "  mcp       : %s",
            ", ".join(
                f"{n} (workspace)" if cfg.servers[n].source == ws_cfg else n
                for n in manager.known_servers()
            ),
        )
    return manager


@dataclass
class ModelsSubsystem:
    """The chat models the daemon runs on, and the settings behind them."""

    triage_settings: Any
    orchestrator_settings: Any
    subagent_settings: Any
    triage: Any
    orchestrator: Any
    subagent: Any
    compaction: Any
    context_settings: Any


async def build_models() -> ModelsSubsystem:
    """Resolve each role's model from the environment.

    Roles are separate so a cheap or local model can own the cheap work:
    ``compaction`` falls back to the main provider settings when unset.
    """
    triage_settings = llm_settings_from_env("triage")
    orchestrator_settings = llm_settings_from_env("orchestrator")
    subagent_settings = llm_settings_from_env("subagent")
    # Triage is a single-shot classifier — reasoning is wasteful here and (on
    # thinking models like gemini-2.5-flash) eats the token budget, truncating
    # the decision JSON. Disable it so the structured output always completes.
    triage = chat_model(triage_settings, temperature=0.0, disable_reasoning=True)
    orchestrator = chat_model(orchestrator_settings, temperature=0.0)
    subagent = chat_model(subagent_settings, temperature=0.1)

    # Warm the Langfuse reachability probe now — before the async host installs
    # blockbuster — so the runtime path never does urllib on the event loop.
    await asyncio.to_thread(warm_reachability_cache)

    compaction = chat_model(llm_settings_from_env("compaction"), temperature=0.0)
    context_settings = ContextSettings.from_env(
        "orchestrator",
        provider=env("LLM_PROVIDER", "orchestrator", "groq"),
    )
    logger.info(
        "  context   : compact >%d tokens, keep %d msgs, offload >%d chars",
        context_settings.compact_trigger_tokens,
        context_settings.keep_messages,
        context_settings.offload_threshold_chars,
    )
    return ModelsSubsystem(
        triage_settings=triage_settings,
        orchestrator_settings=orchestrator_settings,
        subagent_settings=subagent_settings,
        triage=triage,
        orchestrator=orchestrator,
        subagent=subagent,
        compaction=compaction,
        context_settings=context_settings,
    )


@dataclass
class ChannelsSubsystem:
    """Where the daemon talks to a person, and what remembers the asking."""

    router: ChannelRouter
    ask_registry: AskRegistry
    session_origin: SessionOriginMap
    web_hub: WebHub | None
    log_bridge: Any | None


async def build_channels(
    opts: DaemonOptions,
    store: Any,
    *,
    fallbacks: tuple[str | None, ...] = (),
) -> ChannelsSubsystem:
    """Assemble the user-facing channels and the ask record behind them.

    ``fallbacks`` are storage degradations to announce on those channels once
    they exist — a Postgres→SQLite fallback means checkpoints are landing
    somewhere Postgres cannot see, and that is the one failure mode that must
    never be silent.
    """
    router = ChannelRouter(channels=[], primary_name="web")
    # Durable Tier-2 asks. Nothing about an ask expires — the agent is parked on
    # a checkpointed interrupt — so the record has to outlive both the socket
    # that showed it and this process. Hydrated with whatever a previous run
    # left unanswered.
    ask_registry = AskRegistry(store)
    router.ask_registry = ask_registry
    await ask_registry.hydrate()
    # Origin-aware HITL routing is always on: CLI attach and channel plugins
    # both map session ids to their channel.
    session_origin = SessionOriginMap()
    router.session_origin = session_origin

    web_hub: WebHub | None = None
    log_bridge: Any | None = None
    if not opts.headless:
        web_hub = WebHub(store)
        router.channels.append(WebChannel(web_hub))
        # Everything the daemon logs about itself reaches the Logs panel.
        # Attached to the `yuyutsava` logger with no level of its own, so
        # PUT /logs/level (the titlebar dropdown) controls the panel — which
        # otherwise changed stderr only and looked like it did nothing.
        log_bridge = LogBridge(web_hub)
        log_bridge.install()

    # Terminal is always present as a fallback, and is the only channel when
    # headless.
    router.channels.append(TerminalChannel(verbose=opts.verbose))
    if opts.headless:
        router.primary_name = "terminal"

    if opts.voice:
        # Optional, and off by default: it listens to the room.
        try:
            vc = voice_channel_from_env()
            router.channels.append(vc)
            logger.info(
                "  voice     : enabled (TTS=%s STT=%s)",
                os.environ.get("TTS_PROVIDER", "piper"),
                type(vc._stt).__name__,
            )
        except Exception:
            logger.warning("voice channel init failed — running without voice", exc_info=True)

    for reason in fallbacks:
        if reason:
            await router.post_event(
                ChannelEvent(
                    payload=TimelinePayload(line=f"storage: {reason}", cls="event-error"),
                )
            )

    return ChannelsSubsystem(
        router=router,
        ask_registry=ask_registry,
        session_origin=session_origin,
        web_hub=web_hub,
        log_bridge=log_bridge,
    )


@dataclass(frozen=True)
class AsyncSubagentSubsystem:
    """The background-subagent host, its task mirror and its health watcher.

    Five names, all of which stay ``None`` when the feature is off, and every one
    of them read again far from where it is built.

    Bundling them means "async subagents are disabled" is a single fact
    (``enabled``) rather than five independent ``is not None`` checks that could
    drift apart. ``host`` is ``None`` even when *enabled* if this process
    attached to a host another process already owns — a distinction that was
    previously carried only in a comment.
    """

    #: True when YUYUTSAVA_ASYNC_SUBAGENTS is set; everything below is None if not.
    enabled: bool
    #: Set only when THIS process won the first-come-wins race and owns the host.
    host: object | None
    #: The dev-server URL — set whether we own the host or attached to another's.
    host_url: str | None
    mirror: object | None
    watcher: object | None
    attachment: object | None

    @property
    def available(self) -> bool:
        """Whether background delegation can actually be offered.

        Reads ``host_url``, not ``host``: an attached process has no host object
        but can still submit runs — a subtle enough invariant to deserve a name.
        """
        return self.host_url is not None


def _completion_sink(
    task_queue: "asyncio.Queue[OrchestratorTask]",
    launch_index: LaunchIndex,
    mirror: AsyncTaskMirror,
) -> Callable[["MirroredTask", bool, str], Awaitable[None]]:
    """What the watcher calls when a background subagent finishes."""

    async def _wake_master_on_completion(task: "MirroredTask", ok: bool, summary: str) -> None:
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
        # A subagent that produced showable artifacts ends its summary with an
        # "ARTIFACTS: id, id" trailer (see the tinker subagent prompt). Lift the
        # ids so the master can re-show them inline via artifact_show.
        artifact_ids = _parse_artifact_ids(summary)
        await task_queue.put(
            OrchestratorTask(
                proposal_id="",
                event_id="",
                topic="subagent_completion",
                summary=f"{task.agent_name} {'ok' if ok else 'failed'}",
                instruction="",
                subagent_hint="",
                urgency=2,
                kind="subagent_completed",
                origin=origin,
                parent_thread_id=parent,
                completion={
                    "task_id": task.task_id,
                    "agent_name": task.agent_name,
                    "ok": ok,
                    "summary": summary,
                    "artifacts": artifact_ids,
                },
            )
        )
        await mirror.mark_notified(task.task_id)

    return _wake_master_on_completion


async def build_async_subagents(
    *,
    bg_subagent_list: list,
    subagent_settings: LlmSettings,
    checkpointer: object,
    stores: BackgroundStores,
    channels: ChannelRouter,
    task_queue: "asyncio.Queue[OrchestratorTask]",
    launch_index: LaunchIndex,
) -> AsyncSubagentSubsystem:
    """Start (or attach to) the background-subagent host.

    Opt-in via ``YUYUTSAVA_ASYNC_SUBAGENTS=1``; subagents are exposed as
    ``<name>-bg`` peers alongside their sync entries. Returns an all-``None``
    bundle with ``enabled=False`` otherwise, so callers branch on one flag.

    ``task_queue`` and ``launch_index`` are inputs rather than outputs because
    the completion sink enqueues master wake-ups onto that queue — the queue
    must exist before the watcher starts.
    """
    if not env_flag("YUYUTSAVA_ASYNC_SUBAGENTS", true=("1", "true", "yes")):
        logger.info("  async subs: disabled (set YUYUTSAVA_ASYNC_SUBAGENTS=1 to enable)")
        return AsyncSubagentSubsystem(
            enabled=False, host=None, host_url=None, mirror=None, watcher=None, attachment=None
        )
    logger.info("  async subs: enabled (YUYUTSAVA_ASYNC_SUBAGENTS=1)")

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
        # Host-only model instance: the host graphs run on the uvicorn
        # loop in their own thread, and a Gemini client binds to the
        # first loop that uses it (llm/quirks/loop_affinity).
        return build_async_host(
            bg_subagent_list,
            model=chat_model(subagent_settings, temperature=0.1),
            checkpointer=checkpointer,
            allow_blocking=allow_blocking,
            stores=stores,
        )

    # First-come-wins shared host: if another process (typically a CLI chat
    # started before the daemon) already owns the LangGraph dev server, attach
    # to it instead of starting a second one.
    #
    # NOTE: once the host owner starts, it installs blockbuster (unless
    # allow_blocking). From here on, any synchronous blocking I/O run on this
    # event loop (os.replace/mkdir/scandir/socket/sqlite) raises BlockingError
    # — wrap such calls in asyncio.to_thread.
    attachment = await asyncio.to_thread(acquire_or_attach_host, factory=_build_host)
    register_host_cleanup(attachment)
    if attachment.host is not None:
        logger.info(
            "  async host: %s (owner; graphs=%s)", attachment.url, attachment.host.graph_ids
        )
    else:
        logger.info("  async host: %s (attached to running owner)", attachment.url)

    mirror = AsyncTaskMirror()
    watcher = AsyncTaskHealthWatcher(
        mirror=mirror,
        host_url=attachment.url,
        # The watcher builds a fully-formatted AskPrompt (clean title/body via
        # the shared interrupt formatter) and hands it straight to the channel
        # router. (make_ask_handler is for the orchestrator's streaming loop,
        # where the interrupt arrives as a raw dict — passing an AskPrompt to
        # it would stringify the object into the body.)
        ask_handler=channels.post_ask,
        event_sink=channels.post_event,
        agent_path_root="orchestrator",
        completion_sink=_completion_sink(task_queue, launch_index, mirror),
        launch_index=launch_index,
    )
    await watcher.start()
    logger.info("  async watcher: running")
    return AsyncSubagentSubsystem(
        enabled=True,
        host=attachment.host,  # None when attaching to another owner
        host_url=attachment.url,
        mirror=mirror,
        watcher=watcher,
        attachment=attachment,
    )


@dataclass(frozen=True)
class SubagentSubsystem:
    """The daemon's subagent roster, sync and background.

    Two lists rather than one because they are
    genuinely different sets: ``sync`` is what the orchestrator may delegate to
    inline, ``background`` adds the TinkerAgent, which exists **only** as an
    async peer. Keeping them apart here is what stops a caller accidentally
    offering inline tinkering.
    """

    #: name -> agent, for the orchestrator's delegation table.
    sync: dict[str, Any]
    #: Ordered list behind ``sync``, as the async host wants it.
    sync_list: list
    #: ``sync_list`` + the TinkerAgent — the roster the background host serves.
    background: list
    task_runner: Any
    #: Mint a FRESH pair of the store-backed peers for another graph.
    #:
    #: A deepagent spec must not share tool/middleware objects across graphs, so
    #: the chat master cannot reuse the orchestrator's instances — it needs its
    #: own. That requirement is about the *instances*, not the dependency set,
    #: which is what this preserves: the seven shared kwargs were previously
    #: written out a second time at the ConversationManager call site, giving
    #: five hand-maintained copies of one argument list.
    make_peers: Callable[[], list]


def build_subagents(
    *,
    workspace: Path,
    policy: Any,
    consent: Any,
    store: Store,
    skill_registry: SkillRegistry,
    search_config: SearchConfig,
    mcp_manager: Any,
    cap_enforcer: Any,
    memory_store: Any,
    skill_store: Any,
) -> SubagentSubsystem:
    """Build the subagent roster.

    The three sync agents took the same seven keyword arguments each, written
    out three times — 21 lines in which a typo in one of them was a silent
    capability difference between siblings. They share one ``**common`` dict
    now, so a new shared dependency is added once and cannot reach two of three.
    """
    task_runner = TaskRunnerAgent(
        workspace_root=workspace,
        sandbox_root=WorkspaceLayout.for_workspace(workspace).sandbox,
        policy=policy,
        consent=consent,
    )
    common: dict[str, Any] = {
        "skill_registry": skill_registry,
        "can_write_skills": True,
        "mcp_manager": mcp_manager,
        "search_config": search_config,
        "cap_enforcer": cap_enforcer,
        "memory_store": memory_store,
        "skill_store": skill_store,
    }
    sync_list = [
        FileOrganizerAgent(task_runner, store, **common),
        FaceWatcherAgent(task_runner, store, **common),
        # name="general-purpose" suppresses deepagents' built-in default.
        # No events store: it answers questions, it does not act on the timeline.
        GeneralPurposeAgent(task_runner, **common),
    ]

    # Background TinkerAgent: an ASYNC peer of the master only — "tinker
    # on card X in the background" from any chat/voice surface. Deliberately NOT
    # in the sync roster: interactive tinkering has its own per-card bundle
    # (ConversationManager), so the master delegates long jobs rather than
    # running them inline.
    tinker = make_tinker_subagent(
        skill_registry=skill_registry,
        search_config=search_config,
        mcp_manager=mcp_manager,
        cap_enforcer=cap_enforcer,
        memory_store=memory_store,
        skill_store=skill_store,
        policy=policy,
        consent=consent,
    )

    def _make_peers() -> list:
        """Fresh store-backed peers for another graph, same dependencies.

        GeneralPurposeAgent is omitted: ``build_agent_stack`` builds its own.
        """
        return [
            FileOrganizerAgent(task_runner, store, **common),
            FaceWatcherAgent(task_runner, store, **common),
        ]

    return SubagentSubsystem(
        sync={sa.name: sa for sa in sync_list},
        sync_list=sync_list,
        background=[*sync_list, tinker],
        task_runner=task_runner,
        make_peers=_make_peers,
    )


@dataclass(frozen=True)
class ResourceSubsystem:
    """The resource governor: a load monitor and the heavy-task admission gate."""

    monitor: ResourceMonitor
    admission: AdmissionController


def build_resources(channels: ChannelRouter, task_registry: TaskRegistry) -> ResourceSubsystem:
    """Sample system load and gate heavy tasks behind it.

    The monitor samples psutil into a ring (main.py schedules its run loop);
    admission gates heavy tasks (complexity ≥ threshold / heavy hints) behind a
    semaphore + load check in ``OrchestratorLoop._run_task``. The activity probe
    is assigned after construction because the controller it asks "is anything
    running?" needs the monitor first.
    """
    res_settings = ResourceSettings.from_env()
    monitor = ResourceMonitor(res_settings, event_sink=channels.post_event)
    admission = AdmissionController(
        monitor,
        res_settings,
        registry=task_registry,
        event_sink=channels.post_event,
    )
    monitor.activity_probe = lambda: bool(admission.active())
    logger.info(
        "  resources : cpu<%.0f%% mem>%dMB disk>%.0fGB, heavy=complexity≥%d (max %d)",
        res_settings.cpu_high_pct,
        res_settings.mem_min_mb,
        res_settings.disk_min_gb,
        res_settings.heavy_complexity,
        res_settings.max_heavy_tasks,
    )
    return ResourceSubsystem(monitor=monitor, admission=admission)


@dataclass(frozen=True)
class BootState:
    """Everything boot has built before the loops, the plugins and the web app.

    One read-only record of the subsystem records, so each later builder takes
    it whole and reads what it needs, instead of a dozen loose arguments. The
    order of the fields is the order they were built in.
    """

    workspace: Path
    home: Path
    daemon_cfg: DaemonConfig
    storage: StorageSubsystem
    policy: PolicySubsystem
    mcp_manager: MCPClientManager
    retention: RetentionSubsystem
    events: EventsSubsystem
    channels: ChannelsSubsystem
    resources: ResourceSubsystem
    models: ModelsSubsystem
    retrieval: RetrievalSubsystem
    search_config: SearchConfig
    subagents: SubagentSubsystem
    #: The orchestrator's work queue, and the index that links a background
    #: task back to the conversation that started it. Created before the async
    #: block so the watcher's completion sink can enqueue master wake-ups.
    task_queue: asyncio.Queue[OrchestratorTask]
    launch_index: LaunchIndex
    async_subagents: AsyncSubagentSubsystem


async def build_boot_state(opts: DaemonOptions) -> BootState:
    """Build storage through the background-subagent host, in boot order."""
    workspace = opts.workspace.resolve()
    home = state_dir()
    # Materialize <ws>/.yuyutsava — the only place the daemon writes inside
    # its workspace (sync mkdir → off-loop; see storage.paths).
    await asyncio.to_thread(WorkspaceLayout.for_workspace(workspace).ensure)

    daemon_cfg = DaemonConfig.from_env()
    events_cfg = _build_initial_events_config(opts, daemon_cfg)

    st = await build_storage(opts)
    pol = await build_policy(st.events)
    mcp_manager = await build_mcp(workspace)
    ret = await build_retention(
        workspace=workspace,
        storage=st.settings,
        store=st.events,
        artifact_store=st.artifact_store,
        storage_health=st.storage_health,
    )
    ev = await build_events(
        events_cfg=events_cfg,
        daemon_cfg=daemon_cfg,
        store=st.events,
        runtime_settings=pol.runtime_settings,
    )
    fallback_reasons = (st.fallback_reason, ret.checkpointer_saver.fallback_reason)
    chans = await build_channels(opts, st.events, fallbacks=fallback_reasons)
    res = build_resources(chans.router, st.task_registry)
    models = await build_models()
    rtv = await build_retrieval(
        workspace=workspace,
        home=home,
        stores=st.stores,
        mem_settings=st.mem_settings,
        pg_pool=st.pg_pool,
        embedder=st.embedder,
    )
    wire_spillover_recovery(
        st.storage_health,
        st.pg_pool,
        st.events,
        chans.router,
        memory_store=st.memory_store,
        skill_store=rtv.skill_store,
    )

    # ── search config (ws_* tools) ---------------------------------------
    search_config = SearchConfig.from_env()
    available = search_config.is_available()
    if any(available.values()):
        logger.info("  search    : %s", ", ".join(p for p, ok in available.items() if ok))

    subs = build_subagents(
        workspace=workspace,
        policy=pol.policy,
        consent=pol.consent_registry,
        store=st.events,
        skill_registry=rtv.skill_registry,
        search_config=search_config,
        mcp_manager=mcp_manager,
        cap_enforcer=pol.cap_enforcer,
        memory_store=st.memory_store,
        skill_store=rtv.skill_store,
    )
    task_queue: asyncio.Queue[OrchestratorTask] = asyncio.Queue()
    launch_index = LaunchIndex()
    # Opt-in via YUYUTSAVA_ASYNC_SUBAGENTS=1; see build_async_subagents.
    async_subs = await build_async_subagents(
        bg_subagent_list=subs.background,
        subagent_settings=models.subagent_settings,
        checkpointer=ret.checkpointer,
        stores=BackgroundStores(
            artifact_store=st.artifact_store,
            context_settings=models.context_settings,
            summary_store=st.summary_store,
            memory_store=st.memory_store,
        ),
        channels=chans.router,
        task_queue=task_queue,
        launch_index=launch_index,
    )
    return BootState(
        workspace=workspace,
        home=home,
        daemon_cfg=daemon_cfg,
        storage=st,
        policy=pol,
        mcp_manager=mcp_manager,
        retention=ret,
        events=ev,
        channels=chans,
        resources=res,
        models=models,
        retrieval=rtv,
        search_config=search_config,
        subagents=subs,
        task_queue=task_queue,
        launch_index=launch_index,
        async_subagents=async_subs,
    )


@dataclass(frozen=True)
class LoopSubsystem:
    """How work arrives: triage for events, direct submission, the orchestrator."""

    task_submission: TaskSubmissionService
    triage_loop: TriageLoop
    orch_loop: OrchestratorLoop


def build_loops(boot: BootState) -> LoopSubsystem:
    """The triage loop, the task-submission service and the orchestrator loop."""
    st, pol, models, subs = boot.storage, boot.policy, boot.models, boot.subagents
    async_subs = boot.async_subagents
    channels = boot.channels.router
    skill_registry = boot.retrieval.skill_registry
    background = subs.background if async_subs.available else None

    def capabilities_block() -> str:
        """The subagent roster as triage should see it *right now*.

        Rebuilt per call rather than captured at boot: the user can switch a
        dedicated subagent off mid-session, and triage must stop proposing it
        immediately (the orchestrator would only refuse the delegation later).
        """
        return render_capabilities_block(
            list(subs.sync.values()),
            async_subagents=background,
            disabled=pol.runtime_settings.subagents().disabled,
        )

    triage = TriageAgent(models.triage)

    # Direct submissions skip triage, so a light-tier call scores their
    # complexity — only when routing is on (a score nobody routes on is wasted
    # spend). The scorer resolves its model lazily and never raises.
    model_router = st.model_router
    complexity_scorer = (
        ComplexityScorer(lambda: model_router.tier_model("light")) if model_router.enabled else None
    )
    task_submission = TaskSubmissionService(
        registry=st.task_registry,
        task_queue=boot.task_queue,
        store=st.events,
        bus=boot.events.bus,
        proposal_expiry_sec=boot.daemon_cfg.proposal_expiry_sec,
        complexity_scorer=complexity_scorer,
    )
    triage_loop = TriageLoop(
        bus=boot.events.bus,
        store=st.events,
        channels=channels,
        triage=triage,
        capabilities_block=capabilities_block,
        task_queue=boot.task_queue,
        proposal_expiry_sec=boot.daemon_cfg.proposal_expiry_sec,
        skill_registry=skill_registry,
        runtime_settings=pol.runtime_settings,
    )

    # Transcript RAG for the orchestrator master (Postgres only): the same
    # per-turn ConversationInjector recall the chat/tinker masters get, so a
    # resumed orchestrator thread remembers its own swept turns. Same selection
    # the CLI and tinker stacks use — the two-condition guard lives in
    # StoreFactory, not in three call sites.
    orch_transcript_index = st.stores.transcript_index()
    orch_deps = OrchestratorDeps(
        subagents=subs.sync,
        subagent_model=models.subagent,
        channels=channels,
        store=st.events,
        subagent_token_budget=boot.daemon_cfg.subagent_token_budget,
        skill_registry=skill_registry,
        workspace_root=boot.workspace,
        mcp_manager=boot.mcp_manager,
        search_config=boot.search_config,
        cap_enforcer=pol.cap_enforcer,
        async_subagents=background,
        async_host_url=async_subs.host_url,
        async_task_mirror=async_subs.mirror,
        artifact_store=st.artifact_store,
        summary_store=st.summary_store,
        memory_store=st.memory_store,
        transcript_store=st.transcript_store,
        transcript_index=orch_transcript_index,
        context_settings=models.context_settings,
        compaction_model=models.compaction,
        usage_store=st.usage_store,
        runtime_settings=pol.runtime_settings,
    )
    orch_loop = OrchestratorLoop(
        task_queue=boot.task_queue,
        channels=channels,
        store=st.events,
        orchestrator_model=models.orchestrator,
        deps=orch_deps,
        orchestrator_token_budget=boot.daemon_cfg.orchestrator_token_budget,
        checkpointer=boot.retention.checkpointer,
        prefs_injector=pol.prefs_injector,
        skill_store=boot.retrieval.skill_store,
        task_registry=st.task_registry,
        model_router=model_router,
        admission=boot.resources.admission,
        launch_index=boot.launch_index,
    )
    return LoopSubsystem(
        task_submission=task_submission, triage_loop=triage_loop, orch_loop=orch_loop
    )


@dataclass(frozen=True)
class PluginSubsystem:
    """Channel plugins, and the one service that resolves every decision."""

    decision_service: DecisionService
    channel_plugins: ChannelPluginRegistry


async def build_plugins(boot: BootState, loops: LoopSubsystem) -> PluginSubsystem:
    """Start the channel plugins behind one :class:`DecisionService`.

    One DecisionService resolves proposal/ask responses for every surface: the
    HTTP routers and any plugin's inbound loop land on the same code path.
    Waiter maps are registered per surface (WebHub + InboundSink).
    """
    web_hub = boot.channels.web_hub
    decision_service = DecisionService(boot.storage.events)
    if web_hub is not None:
        decision_service.add_waiters(
            proposals=web_hub.pending_proposals,
            asks=web_hub.pending_asks,
        )

    def _daemon_status() -> str:
        return (
            f"daemon: running — web http://{boot.daemon_cfg.web_host}:"
            f"{boot.daemon_cfg.web_port}/ · subagents: {', '.join(boot.subagents.sync)}"
        )

    inbound_sink = InboundSink(
        task_submission=loops.task_submission,
        decision_service=decision_service,
        task_registry=boot.storage.task_registry,
        prefs_store=boot.policy.prefs_store,
        status_provider=_daemon_status,
    )
    decision_service.add_waiters(
        proposals=inbound_sink.pending_proposals,
        asks=inbound_sink.pending_asks,
    )
    channel_plugins = ChannelPluginRegistry(
        router=boot.channels.router,
        sink=inbound_sink,
        config=ChannelsConfig.from_file(),
    )
    await channel_plugins.start_all()
    return PluginSubsystem(decision_service=decision_service, channel_plugins=channel_plugins)


async def build_web(
    boot: BootState, loops: LoopSubsystem, plugins: PluginSubsystem
) -> uvicorn.Server | None:
    """The web app and its server, or ``None`` without a web hub (``--no-ui``)."""
    web_hub = boot.channels.web_hub
    if web_hub is None:
        return None
    st, pol, async_subs = boot.storage, boot.policy, boot.async_subagents
    cfg = boot.daemon_cfg
    # from_env may mkdir + write the api_token file on a non-loopback bind →
    # off-loop (this runs after the subagent host activates blockbuster).
    auth_settings = await asyncio.to_thread(AuthSettings.from_env, host=cfg.web_host)
    if auth_settings.enforce:
        logger.info("  web auth  : bearer token enforced (non-loopback bind %s)", cfg.web_host)
    # Lazy host for interactive text/voice conversations (Electron/mobile).
    # Builds its shared deepagent bundle on first /ws/converse use and
    # attaches to this daemon's async-subagent host (no second host).
    # Fresh subagent instances for the chat master's sync roster — the
    # orchestrator's own instances stay on its graph (specs must not
    # share tool/middleware objects across graphs). GeneralPurposeAgent
    # is built inside build_agent_stack; these two join it.
    conversation_manager = ConversationManager(
        workspace=boot.workspace,
        checkpointer=boot.retention.checkpointer,
        settings=boot.models.orchestrator_settings,
        search_config=boot.search_config,
        voice_store=st.voice_store,
        usage_store=st.usage_store,
        mcp_manager=boot.mcp_manager,
        launch_index=boot.launch_index,
        prefs_store=pol.prefs_store,
        runtime_settings=pol.runtime_settings,
        cap_enforcer=pol.cap_enforcer,
        task_submission=loops.task_submission,
        extra_subagents=boot.subagents.make_peers(),
    )

    # An ask answered after a restart has no waiter left to wake — the
    # agent is parked on a checkpointed interrupt, so the answer has to
    # re-enter the graph. Built here because it needs both the conversation
    # manager (for conversation-owned asks) and the watcher (for background
    # ones), and handed to the DecisionService every responder goes through.
    ask_resume = AskResumeService(
        registry=boot.channels.ask_registry,
        conversation_manager=conversation_manager,
        watcher=async_subs.watcher,
        channels=boot.channels.router,
    )
    plugins.decision_service.set_ask_resume(ask_resume)

    app = create_app(
        web_hub,
        host=cfg.web_host,
        deps=AppDeps(
            skill_registry=boot.retrieval.skill_registry,
            config_reload=boot.events.reload,
            channels=boot.channels.router,
            session_origin=boot.channels.session_origin,
            auth=auth_settings,
            task_registry=st.task_registry,
            task_submission=loops.task_submission,
            decision_service=plugins.decision_service,
            ask_registry=boot.channels.ask_registry,
            ask_resume=ask_resume,
            channel_plugins=plugins.channel_plugins,
            usage_store=st.usage_store,
            resource_monitor=boot.resources.monitor,
            admission_controller=boot.resources.admission,
            model_router=st.model_router,
            memory_store=st.memory_store,
            conversation_manager=conversation_manager,
            voice_store=st.voice_store,
            transcript_store=st.transcript_store,
            async_subagents=async_subs.available,
            async_task_watcher=async_subs.watcher,
            runtime_settings=pol.runtime_settings,
            subagent_roster=boot.subagents.sync,
        ),
    )
    uvicorn_level = logging.getLevelName(logging.getLogger("yuyutsava").getEffectiveLevel()).lower()
    config = uvicorn.Config(
        app,
        host=cfg.web_host,
        port=cfg.web_port,
        log_level=uvicorn_level,
        lifespan="on",
        # uvicorn's access log records full request lines including the
        # query string — which carries ?token= on /stream when auth is
        # enforced. Drop it off-loopback; the in-app HTTP log middleware
        # (path-only) still covers observability.
        access_log=not auth_settings.enforce,
    )
    return uvicorn.Server(config)


def _web_url(cfg: DaemonConfig) -> str:
    """The local web window's address.

    A wildcard bind (0.0.0.0 / ::) is not an openable address; show loopback
    for the local web window. Remote clients use the host's tailnet IP.
    """
    host = cfg.web_host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{cfg.web_port}/"


async def build_daemon(opts: DaemonOptions) -> DaemonSubsystems:
    """Wire every daemon subsystem and return them as a populated record.

    Boot order: store → prefs/policy → MCP → checkpointer/sweeper → bus and
    sources → channels → resources → models → skills → search → subagents →
    background host → loops → channel plugins → web app. The caller is
    responsible for installing signal handlers, scheduling the loop tasks, and
    ordered shutdown.
    """
    boot = await build_boot_state(opts)
    loops = build_loops(boot)
    plugins = await build_plugins(boot, loops)
    web_server = await build_web(boot, loops, plugins)
    st, chans, async_subs = boot.storage, boot.channels, boot.async_subagents
    return DaemonSubsystems(
        daemon_cfg=boot.daemon_cfg,
        home=boot.home,
        workspace=boot.workspace,
        store=st.events,
        prefs_store=boot.policy.prefs_store,
        runtime_settings=boot.policy.runtime_settings,
        bus=boot.events.bus,
        registry=boot.events.registry,
        channels=chans.router,
        web_hub=chans.web_hub,
        log_bridge=chans.log_bridge,
        web_server=web_server,
        web_url=_web_url(boot.daemon_cfg),
        mcp_manager=boot.mcp_manager,
        checkpointer_saver=boot.retention.checkpointer_saver,
        sweeper=boot.retention.sweeper,
        pg_pool=st.pg_pool,
        storage_health=st.storage_health,
        artifact_store=st.artifact_store,
        summary_store=st.summary_store,
        transcript_store=st.transcript_store,
        memory_store=st.memory_store,
        embedder=st.embedder,
        task_queue=boot.task_queue,
        triage_loop=loops.triage_loop,
        orch_loop=loops.orch_loop,
        task_registry=st.task_registry,
        task_submission=loops.task_submission,
        decision_service=plugins.decision_service,
        channel_plugins=plugins.channel_plugins,
        usage_store=st.usage_store,
        model_router=st.model_router,
        resource_monitor=boot.resources.monitor,
        admission=boot.resources.admission,
        skill_registry=boot.retrieval.skill_registry,
        triage_settings=boot.models.triage_settings,
        orchestrator_settings=boot.models.orchestrator_settings,
        subagent_names=tuple(boot.subagents.sync.keys()),
        async_host=async_subs.host,
        async_task_mirror=async_subs.mirror,
        async_task_watcher=async_subs.watcher,
        session_origin=chans.session_origin,
        async_host_attachment=async_subs.attachment,
        hot_reload_events_config=boot.events.reload,
    )
