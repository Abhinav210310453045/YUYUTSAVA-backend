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
from yuyutsava.core.config import (
    DaemonConfig, EventsConfig, LlmSettings, SearchConfig, SourceConfig,
    llm_settings_from_env,
)
from yuyutsava.core.llm import chat_model
from yuyutsava.core.policy import PermissionsPolicy, StorePolicyCapEnforcer
from yuyutsava.daemon.channels import ChannelRouter
from yuyutsava.daemon.checkpointing import CheckpointerSaver
from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop
from yuyutsava.daemon.terminal_channel import TerminalChannel
from yuyutsava.daemon.triage_loop import OrchestratorTask, TriageLoop
from yuyutsava.daemon.web.server import WebChannel, WebHub, make_app
from yuyutsava.events.bus import EventBus
from yuyutsava.events.registry import SourceRegistry
from yuyutsava.mcp.config import MCPConfig
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.prefs.injector import PrefsInjector
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.storage.events import Store
from yuyutsava.storage.paths import blobs_dir, checkpoints_db_path, state_dir
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

    # subagents + queue + loops
    task_queue: asyncio.Queue[OrchestratorTask]
    triage_loop: TriageLoop
    orch_loop: OrchestratorLoop

    # for logging / future use
    skill_registry: SkillRegistry
    triage_settings: LlmSettings
    orchestrator_settings: LlmSettings
    subagent_names: tuple[str, ...]

    # hot reload — closure over registry + daemon_cfg; reload-loop calls it.
    hot_reload_events_config: Callable[[], Awaitable[None]]


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

    # ── checkpointer (SQLite-backed; sweeper handles stale threads) -------
    checkpointer_saver = CheckpointerSaver(db_path=checkpoints_db_path())
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

    # ── models ------------------------------------------------------------
    triage_settings = llm_settings_from_env("triage")
    orchestrator_settings = llm_settings_from_env("orchestrator")
    subagent_settings = llm_settings_from_env("subagent")
    triage_model = chat_model(triage_settings, temperature=0.0)
    orchestrator_model = chat_model(orchestrator_settings, temperature=0.0)
    subagent_model = chat_model(subagent_settings, temperature=0.1)

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
    capabilities_block = render_capabilities_block(list(subagents.values()))

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
    )

    # ── web server -------------------------------------------------------
    if web_hub is not None:
        app = make_app(
            web_hub,
            host=daemon_cfg.web_host,
            skill_registry=skill_registry,
            config_reload=_hot_reload_events_config,
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
        task_queue=task_queue,
        triage_loop=triage_loop,
        orch_loop=orch_loop,
        skill_registry=skill_registry,
        triage_settings=triage_settings,
        orchestrator_settings=orchestrator_settings,
        subagent_names=tuple(subagents.keys()),
        hot_reload_events_config=_hot_reload_events_config,
    )
