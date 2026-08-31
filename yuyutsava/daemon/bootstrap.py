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
from yuyutsava.consent import ConsentRegistry
from yuyutsava.agents.triage.agent import TriageAgent
from yuyutsava.async_subagents.launch_index import LaunchIndex
from yuyutsava.async_subagents.session_origin import SessionOriginMap
from yuyutsava.channels.config import ChannelsConfig
from yuyutsava.channels.plugin import InboundSink
from yuyutsava.channels.registry import ChannelPluginRegistry
from yuyutsava.context.config import ContextSettings
from yuyutsava.core.config import (
    DaemonConfig, EventsConfig, LlmSettings, SearchConfig, SourceConfig,
    _env, llm_settings_from_env,
)
from yuyutsava.llm import chat_model
from yuyutsava.core.model_router import ComplexityScorer, ModelRouter
from yuyutsava.core.policy import PermissionsPolicy, StorePolicyCapEnforcer
from yuyutsava.daemon.ask_registry import AskRegistry
from yuyutsava.daemon.ask_resume import AskResumeService
from yuyutsava.daemon.channels import ChannelEvent, ChannelRouter, TimelinePayload
from yuyutsava.daemon.checkpointing import CheckpointerSaver
from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop
from yuyutsava.daemon.resources import AdmissionController, ResourceMonitor, ResourceSettings
from yuyutsava.daemon.task_registry import TaskRegistry
from yuyutsava.daemon.task_submission import TaskSubmissionService
from yuyutsava.daemon.terminal_channel import TerminalChannel
from yuyutsava.daemon.triage_loop import OrchestratorTask, TriageLoop
from yuyutsava.daemon.usage import UsageStore
from yuyutsava.daemon.web.auth import AuthSettings
from yuyutsava.daemon.web.server import WebChannel, WebHub, make_app
from yuyutsava.daemon.web.services.decision_service import DecisionService
from yuyutsava.events.bus import EventBus
from yuyutsava.events.registry import SourceRegistry
from yuyutsava.mcp.config import MCPConfig
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.memory.config import MemorySettings
from yuyutsava.memory.embedder import Embedder
from yuyutsava.prefs.injector import PrefsInjector
from yuyutsava.prefs.runtime import RuntimeSettings
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.skills.store import SkillIndexer
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.memory.store import MemoryStore
from yuyutsava.storage.factory import StoreFactory
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
    # Hot toggles (voice mode, subagent deny-list) shared by the web API, the
    # wake bridge, the conversation manager and the agent middleware.
    runtime_settings: RuntimeSettings

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
    return EventsConfig(sources={
        **events_cfg.sources,
        "voice": SourceConfig(name="voice", enabled=wake_enabled, params=dict(src.params)),
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


# ---------------------------------------------------------------------------
# Subsystem builders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageSubsystem:
    """Everything the persistence layer produces, as one explicit record.

    Phase 3 step 3.3 (ADR-003). ``build_daemon`` was a 927-line function whose
    only description of how the system fits together was *statement order inside
    one body* — unverifiable by any tool, unholdable in one head.

    This is the first slice pulled out. It has explicit inputs (options) and
    explicit outputs (this record), so it can be built and inspected without
    constructing a daemon, and the load order it depends on is visible in a
    signature rather than implied by position.
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


async def build_storage(opts: DaemonOptions) -> StorageSubsystem:
    """Open the backend, run migrations, and construct every domain store.

    Extracted verbatim from ``build_daemon``. The only change is that its
    outputs are returned as a record instead of left as locals for the next 600
    lines to read.
    """
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

    # ── stores -------------------------------------------------------------
    # One StoreFactory resolves "Postgres or SQLite" ONCE (ADR-002 step 2.6).
    # This block used to re-decide it inline for every store — thirteen separate
    # `if pg_pool is not None` branches — and separately wrapped three of them
    # in RoutedStore with nothing recording why. Both now live in
    # yuyutsava/storage/factory.py, with failover declared per domain in
    # storage/domains.py.
    #
    # storage_health is built below (it needs pg_pool), so the factory is
    # created with it in a moment; the stores that want spillover are requested
    # after that point.
    storage_health = StorageHealth(pg_pool) if pg_pool is not None else None
    stores = StoreFactory(
        storage, pg_pool=pg_pool, health=storage_health, embedder=embedder,
    )

    artifact_store = stores.artifacts(semantic_recall=_semantic_recall)
    summary_store = stores.summaries()
    transcript_store = stores.transcripts()
    voice_store = stores.voice()
    if pg_pool is not None:
        # Sessions move to Postgres too (migration v6). Inject the shared pool
        # so the web router's get_default_session_store() reuses it; migrations
        # already ran above, so the store skips its own lazy schema-ensure.
        set_default_session_store(PgSessionStore(storage, pool=pg_pool))

    # ── task registry (Phase 2: first-class task tracking) -----------------
    task_registry = TaskRegistry(stores.tasks())

    # ── usage accounting + model routing (Phase 4) --------------------------
    usage_store: UsageStore = stores.usage()
    model_router = ModelRouter.from_env()
    if model_router.enabled:
        logger.info("  routing   : complexity-based model routing enabled")

    # ── semantic memory (default-on when postgres is live) -----------------
    # mem_settings + embedder were built above (shared with the artifact recall
    # index); memory and skills reuse the same embedder instance.
    memory_store: MemoryStore | None = stores.memory(mem_settings)
    # (Memory recall for the orchestrator master moved into build_orchestrator
    # as a per-turn RetrievalInjectionPolicy — no build-time injector here.)

    # ── store (events DB: postgres-primary + sqlite spillover buffer) ------
    # On the Postgres backend each domain becomes a RoutedStore that fails over
    # to a SQLite buffer when Postgres is unreachable; the health probe drains
    # the buffer back on recovery (storage/routing). SQLite mode keeps the
    # SQLite twins as the permanent primary.
    store = stores.events()
    await store.start()

    # ── visuals + feedback default stores (Postgres-primary, spillover) ----
    # These are the REST-path stores written OUTSIDE a checkpointed turn (the
    # 👍/👎 endpoint, the /visuals API), so a Postgres blip here would otherwise
    # lose the write. Wrap the Pg store + SQLite twin in the shared RoutedStore
    # so an outage buffers to SQLite and the Reconciler (below) drains it back on
    # recovery. On the SQLite backend the getters fall back to the twins lazily.
    if pg_pool is not None and storage_health is not None:
        from yuyutsava.storage.feedback_store import set_default_feedback_store
        from yuyutsava.todoboard.store import set_default_todo_store
        from yuyutsava.visuals.store import set_default_visual_store

        # Spillover is now a DECLARED policy (storage/domains.py: Failover),
        # not a wiring accident. The factory wraps exactly the domains that ask
        # for it; these three are the REST-path stores written OUTSIDE a
        # checkpointed turn, where a raised error loses the write outright.
        set_default_visual_store(stores.visuals())
        set_default_feedback_store(stores.feedback())
        set_default_todo_store(stores.todos())


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

    Phase 3 step 3.3, second slice. Cohesive because all three read from the
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
    install process-global singletons (finding ``F-S08``). Those are Phase 3
    step 3.4; leaving them here keeps this extraction behaviour-preserving, and
    the globals are now at least confined to one named function instead of
    being buried mid-way through a 900-line body.
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
            _voice_toggles.wake_enabled, _voice_toggles.tts_enabled,
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

    Phase 3 step 3.3, third slice. The checkpointer and sweeper belong together:
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

    Inputs are explicit rather than read from enclosing scope — which is the
    point of the extraction. The sweeper's dependency on the artifact store and
    the health handle used to be invisible; now it is in the signature.
    """
    # ── checkpointer (sqlite or postgres; sweeper handles stale threads) --
    checkpointer_saver = CheckpointerSaver(db_path=checkpoints_db_path(), storage=storage)
    checkpointer = await checkpointer_saver.start()

    from yuyutsava.todoboard.exchange import get_default_exchange

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

    Phase 3 step 3.3, fourth slice — and the one that closes a real coverage
    gap. The note index is built inside
    ``if pg_pool is not None and embedder is not None``, a **Postgres-only**
    branch that no test could reach while it lived inside ``build_daemon``
    (which starts servers and never returns). That is exactly the branch where
    finding Y's ``NameError`` hid.

    Extracted, it is directly testable on a live Postgres.
    """

    skill_registry: SkillRegistry
    skill_store: object
    #: ``TodoNoteIndex`` on Postgres+embedder; ``None`` otherwise, which leaves
    #: the exchange's embed-on-write hooks no-oping.
    note_index: object | None


async def build_retrieval(
    *,
    home: Path,
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
    skill_registry = SkillRegistry(home_dir=home / "skills")
    logger.info("  skills    : %d bundled, scanning personal + workspace",
                len([s for s in skill_registry.scan() if s.scope == "bundled"]))

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
        # Imported here, not inherited from an enclosing block: the
        # build_retention extraction (Phase 3 step 3.3) moved the previous
        # import into that function's scope, leaving this use unbound. It only
        # fires on the Postgres path, so no SQLite test caught it — the static
        # unbound-name check did.
        from yuyutsava.todoboard.exchange import get_default_exchange
        from yuyutsava.todoboard.recall import TodoNoteIndex, set_default_note_index

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

    Phase 3 step 3.3, fifth slice. ``reload`` is returned as a callable rather
    than left as a closure in ``build_daemon``'s body: the SIGHUP handler needs
    it, and a returned function is visible in a signature where a closure buried
    600 lines up was not.
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
            logger.info("config reload: events sources now %s",
                        ", ".join(new_cfg.sources.keys()) or "(none)")


    return EventsSubsystem(
        bus=bus,
        registry=registry,
        events_cfg=events_cfg,
        reload=_hot_reload_events_config,
    )



@dataclass(frozen=True)
class AsyncSubagentSubsystem:
    """The background-subagent host, its task mirror and its health watcher.

    Phase 3 step 3.3, sixth slice — the **largest** block in ``build_daemon``
    (152 lines) and the one with the most tangled outputs: five names, all of
    which stay ``None`` when the feature is off, every one of them read again
    hundreds of lines further down.

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
        but can still submit runs. ``build_daemon`` made this distinction
        correctly in three places by writing ``async_host_url is not None`` — a
        subtle enough invariant to deserve a name.
        """
        return self.host_url is not None


async def build_async_subagents(
    *,
    bg_subagent_list: list,
    subagent_settings: LlmSettings,
    checkpointer: object,
    artifact_store: object | None,
    context_settings: ContextSettings,
    summary_store: object | None,
    memory_store: object | None,
    channels: ChannelRouter,
    task_queue: "asyncio.Queue[OrchestratorTask]",
    launch_index: LaunchIndex,
) -> AsyncSubagentSubsystem:
    """Start (or attach to) the background-subagent host.

    Opt-in via ``YUYUTSAVA_ASYNC_SUBAGENTS=1``. Returns an all-``None`` bundle
    with ``enabled=False`` otherwise, so callers branch on one flag.

    ``task_queue`` and ``launch_index`` are inputs rather than outputs because
    the completion sink defined here enqueues master wake-ups onto that queue —
    the queue must exist before the watcher starts.
    """
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
    enabled = os.environ.get("YUYUTSAVA_ASYNC_SUBAGENTS", "").lower() in ("1", "true", "yes")
    if enabled:
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
            # Background graphs get the same context controllers as the
            # masters (tool-result offload + compaction + ctx_* readback) —
            # bg runs are the longest and were the only agents without them.
            from yuyutsava.context.tools import make_context_tools
            from yuyutsava.core.engine import context_middleware

            # Host-only model instances — the host graphs run on the uvicorn
            # loop in the async-subagent-host thread, and Gemini SDK clients
            # bind to the first loop that uses them, so the main loop's
            # subagent/compaction models must never serve host runs (see
            # llm/quirks/loop_affinity + Architecture.md "Event-loop ownership").
            host_subagent_model = chat_model(subagent_settings, temperature=0.1)
            host_compaction_model = chat_model(
                llm_settings_from_env("compaction"), temperature=0.0
            )

            return AsyncSubagentHost.from_subagents(
                bg_subagent_list,
                model=host_subagent_model,
                checkpointer=checkpointer,
                allow_blocking=allow_blocking,
                middleware_factory=lambda sa: context_middleware(
                    model=host_subagent_model,
                    artifact_store=artifact_store,
                    context_settings=context_settings,
                    summary_store=summary_store,
                    memory_store=memory_store,
                    transcript_store=None,  # bg thread ids are host-minted;
                    # transcripts serve interactive resume — skip them here.
                    compaction_model=host_compaction_model,
                    role=f"{sa.name}-bg",
                ),
                extra_tools_factory=(
                    (lambda: make_context_tools(artifact_store))
                    if artifact_store is not None else None
                ),
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
            # A subagent that produced showable artifacts ends its summary with an
            # "ARTIFACTS: id, id" trailer (see the tinker subagent prompt). Lift the
            # ids so the master can re-show them inline via artifact_show.
            artifact_ids = _parse_artifact_ids(summary)
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
                    "artifacts": artifact_ids,
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

    return AsyncSubagentSubsystem(
        enabled=enabled,
        host=async_host,
        host_url=async_host_url,
        mirror=async_mirror,
        watcher=async_watcher,
        attachment=async_host_attachment,
    )



@dataclass(frozen=True)
class SubagentSubsystem:
    """The daemon's subagent roster, sync and background.

    Phase 3 step 3.3, seventh slice. Two lists rather than one because they are
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
        workspace_root=workspace, policy=policy, consent=consent,
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

    # Background TinkerAgent (Phase 6): an ASYNC peer of the master only — "tinker
    # on card X in the background" from any chat/voice surface. Deliberately NOT
    # in the sync roster: interactive tinkering has its own per-card bundle
    # (ConversationManager), so the master delegates long jobs rather than
    # running them inline.
    from yuyutsava.agents.tinker.subagent import make_tinker_subagent

    tinker = make_tinker_subagent(
        skill_registry=skill_registry, search_config=search_config,
        mcp_manager=mcp_manager, cap_enforcer=cap_enforcer,
        memory_store=memory_store, skill_store=skill_store,
        policy=policy, consent=consent,
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

    # ── storage (extracted: build_storage) ---------------------------------
    _st = await build_storage(opts)
    storage = _st.settings
    pg_pool = _st.pg_pool
    storage_health = _st.storage_health
    stores = _st.stores
    embedder = _st.embedder
    mem_settings = _st.mem_settings
    artifact_store = _st.artifact_store
    summary_store = _st.summary_store
    transcript_store = _st.transcript_store
    voice_store = _st.voice_store
    memory_store = _st.memory_store
    usage_store = _st.usage_store
    task_registry = _st.task_registry
    store = _st.events
    model_router = _st.model_router
    storage_fallback_reason = _st.fallback_reason

    # ── policy / consent / prefs (extracted: build_policy) -----------------
    _pol = await build_policy(store)
    prefs_store = _pol.prefs_store
    prefs_injector = _pol.prefs_injector
    runtime_settings = _pol.runtime_settings
    policy = _pol.policy
    cap_enforcer = _pol.cap_enforcer
    consent_registry = _pol.consent_registry

    # ── MCP servers -------------------------------------------------------
    mcp_manager = MCPClientManager()
    mcp_cfg = MCPConfig.from_file()
    await mcp_manager.start(mcp_cfg)
    if mcp_manager.known_servers():
        logger.info("  mcp       : %s", ", ".join(mcp_manager.known_servers()))

    # ── retention: checkpointer + TTL sweeper (extracted: build_retention) --
    _ret = await build_retention(
        workspace=workspace, storage=storage, store=store,
        artifact_store=artifact_store, storage_health=storage_health,
    )
    checkpointer_saver = _ret.checkpointer_saver
    checkpointer = _ret.checkpointer
    sweeper = _ret.sweeper

    # ── events: bus + sources + hot reload (extracted: build_events) -------
    _ev = await build_events(
        events_cfg=events_cfg, daemon_cfg=daemon_cfg,
        store=store, runtime_settings=runtime_settings,
    )
    bus = _ev.bus
    registry = _ev.registry
    events_cfg = _ev.events_cfg
    _hot_reload_events_config = _ev.reload

    # ── channels ----------------------------------------------------------
    channels = ChannelRouter(channels=[], primary_name="web")
    # Durable Tier-2 asks. Nothing about an ask expires — the agent is parked on
    # a checkpointed interrupt — so the record has to outlive both the socket
    # that showed it and this process. Hydrated below with whatever a previous
    # run left unanswered.
    ask_registry = AskRegistry(store)
    channels.ask_registry = ask_registry
    await ask_registry.hydrate()
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

    # ── retrieval: skills + TODO-note indexes (extracted: build_retrieval) --
    _rtv = await build_retrieval(
        home=home, stores=stores, mem_settings=mem_settings,
        pg_pool=pg_pool, embedder=embedder,
    )
    skill_registry = _rtv.skill_registry
    skill_store = _rtv.skill_store
    note_index = _rtv.note_index

    # ── storage spillover recovery -----------------------------------------
    # On Postgres recovery the health probe drains the buffered SQLite rows back
    # into Postgres (drain-and-delete: no duplication) and re-embeds any
    # vector-less memory/skill rows via their backfill(). The degrade notifier
    # surfaces the outage on the user timeline (never silently divergent).
    if storage_health is not None and pg_pool is not None:
        # Both stores declare backfill_embeddings unconditionally since
        # ADR-002 step 2.5b (it returns 0 without vectors), so no getattr probe.
        # `memory_store` can still be None when memory is switched off.
        _backfills = [
            s.backfill_embeddings
            for s in (memory_store, skill_store) if s is not None
        ]
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
    subs = build_subagents(
        workspace=workspace, policy=policy, consent=consent_registry,
        store=store, skill_registry=skill_registry, search_config=search_config,
        mcp_manager=mcp_manager, cap_enforcer=cap_enforcer,
        memory_store=memory_store, skill_store=skill_store,
    )
    task_runner = subs.task_runner
    subagent_list, subagents = subs.sync_list, subs.sync
    bg_subagent_list = subs.background

    # Orchestrator work queue + the launch index that links a background task
    # back to the conversation that started it. Created before the async block so
    # the watcher's completion sink can enqueue master wake-ups onto the queue.
    task_queue: asyncio.Queue[OrchestratorTask] = asyncio.Queue()
    launch_index = LaunchIndex()

    # ── async (background) subagent host + mirror + watcher --------------
    # Opt-in via YUYUTSAVA_ASYNC_SUBAGENTS=1; see build_async_subagents.
    async_subs = await build_async_subagents(
        bg_subagent_list=bg_subagent_list,
        subagent_settings=subagent_settings,
        checkpointer=checkpointer,
        artifact_store=artifact_store,
        context_settings=context_settings,
        summary_store=summary_store,
        memory_store=memory_store,
        channels=channels,
        task_queue=task_queue,
        launch_index=launch_index,
    )

    def capabilities_block() -> str:
        """The subagent roster as triage should see it *right now*.

        Rebuilt per call rather than captured at boot: the user can switch a
        dedicated subagent off mid-session, and triage must stop proposing it
        immediately (the orchestrator would only refuse the delegation later).
        """
        return render_capabilities_block(
            list(subagents.values()),
            async_subagents=bg_subagent_list if async_subs.available else None,
            disabled=runtime_settings.subagents().disabled,
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
        runtime_settings=runtime_settings,
    )

    # Transcript RAG for the orchestrator master (Postgres only): the same
    # per-turn ConversationInjector recall the chat/tinker masters get, so a
    # resumed orchestrator thread remembers its own swept turns.
    # Same selection the CLI and tinker stacks use (Phase 3 step 3.5) — the
    # two-condition guard lives in StoreFactory, not in three call sites.
    orch_transcript_index = stores.transcript_index()

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
        async_subagents=bg_subagent_list if async_subs.available else None,
        async_host_url=async_subs.host_url,
        async_task_mirror=async_subs.mirror,
        artifact_store=artifact_store,
        summary_store=summary_store,
        memory_store=memory_store,
        transcript_store=transcript_store,
        transcript_index=orch_transcript_index,
        context_settings=context_settings,
        compaction_model=compaction_model,
        usage_store=usage_store,
        runtime_settings=runtime_settings,
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
        # Fresh subagent instances for the chat master's sync roster — the
        # orchestrator's own instances stay on its graph (specs must not
        # share tool/middleware objects across graphs). GeneralPurposeAgent
        # is built inside build_agent_stack; these two join it.
        chat_extra_subagents = subs.make_peers()
        conversation_manager = ConversationManager(
            workspace=workspace,
            checkpointer=checkpointer,
            settings=orchestrator_settings,
            search_config=search_config,
            voice_store=voice_store,
            usage_store=usage_store,
            mcp_manager=mcp_manager,
            launch_index=launch_index,
            prefs_store=prefs_store,
            runtime_settings=runtime_settings,
            cap_enforcer=cap_enforcer,
            task_submission=task_submission,
            extra_subagents=chat_extra_subagents,
        )

        # An ask answered after a restart has no waiter left to wake — the
        # agent is parked on a checkpointed interrupt, so the answer has to
        # re-enter the graph. Built here because it needs both the conversation
        # manager (for conversation-owned asks) and the watcher (for background
        # ones), and handed to the DecisionService every responder goes through.
        ask_resume = AskResumeService(
            registry=ask_registry,
            conversation_manager=conversation_manager,
            watcher=async_subs.watcher,
            channels=channels,
        )
        decision_service.set_ask_resume(ask_resume)

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
            ask_registry=ask_registry,
            ask_resume=ask_resume,
            channel_plugins=channel_plugins,
            usage_store=usage_store,
            resource_monitor=resource_monitor,
            admission_controller=admission,
            model_router=model_router,
            memory_store=memory_store,
            conversation_manager=conversation_manager,
            voice_store=voice_store,
            transcript_store=transcript_store,
            async_subagents=async_subs.available,
            async_task_watcher=async_subs.watcher,
            runtime_settings=runtime_settings,
            subagent_roster=subagents,
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
        runtime_settings=runtime_settings,
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
        async_host=async_subs.host,
        async_task_mirror=async_subs.mirror,
        async_task_watcher=async_subs.watcher,
        session_origin=session_origin,
        async_host_attachment=async_subs.attachment,
        hot_reload_events_config=_hot_reload_events_config,
    )
