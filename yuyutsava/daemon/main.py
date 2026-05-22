"""
Daemon entry point — wires every component and runs the loops concurrently.

Boot order:
    .env → configs → store → bus → sources → channels → web server
        → triage agent + loop → orchestrator deps + loop → run.

Shutdown order (on SIGINT/SIGTERM):
    sources → triage loop → orchestrator loop (drain in-flight) → web server
        → channels → store.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
from pathlib import Path

import uvicorn

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

from yuyutsava.agents.face_watcher.agent import FaceWatcherAgent
from yuyutsava.agents.file_organizer.agent import FileOrganizerAgent
from yuyutsava.agents.general_purpose.agent import GeneralPurposeAgent
from yuyutsava.agents.orchestrator.agent import OrchestratorDeps
from yuyutsava.agents.orchestrator.capabilities import render_capabilities_block
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.triage.agent import TriageAgent
from yuyutsava.core.config import (
    DaemonConfig, EventsConfig, SearchConfig, llm_settings_from_env, yuyutsava_home,
)
from yuyutsava.mcp.config import MCPConfig
from yuyutsava.mcp.loader import MCPClientManager
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.core.llm import chat_model
from yuyutsava.agents.task_runner.tools import set_default_policy
from yuyutsava.daemon.blob_sweeper import BlobSweeper, BlobSweepTarget
from yuyutsava.daemon.events_sweeper import EventsSweeper
from yuyutsava.daemon.channels import ChannelRouter
from yuyutsava.daemon.checkpointing import CheckpointerManager
from yuyutsava.daemon.lifecycle import install_reload_handler, install_signal_handlers
from yuyutsava.daemon.permissions_policy import PermissionsPolicy, StorePolicyCapEnforcer
from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop
from yuyutsava.daemon.terminal_channel import TerminalChannel
from yuyutsava.daemon.triage_loop import OrchestratorTask, TriageLoop
from yuyutsava.daemon.web.server import WebChannel, WebHub, make_app
from yuyutsava.prefs.store import UserPrefsStore
from yuyutsava.prefs.injector import PrefsInjector
from yuyutsava.events.bus import EventBus
from yuyutsava.events.registry import SourceRegistry
from yuyutsava.events.store import Store

logger = logging.getLogger("yuyutsava.daemon")


_LOG_LEVEL_NAMES = ("DEBUG", "INFO", "WARNING")


def _resolve_level(name: str | None, fallback: int) -> int:
    if not name:
        return fallback
    upper = name.upper()
    if upper not in _LOG_LEVEL_NAMES:
        return fallback
    return getattr(logging, upper)


def _setup_logging(verbose: bool, persisted_level: str | None = None) -> None:
    # CLI --verbose forces DEBUG; otherwise use persisted pref, else INFO.
    if verbose:
        level = logging.DEBUG
    else:
        level = _resolve_level(persisted_level, logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s",
                                           datefmt="%H:%M:%S"))
    root = logging.getLogger("yuyutsava")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
    # Mirror the level to uvicorn so HTTP request logs follow the same knob.
    logging.getLogger("uvicorn").setLevel(level)
    logging.getLogger("uvicorn.error").setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(level)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yuyutsava daemon",
        description="Run the always-on YUYUTSAVA daemon.",
    )
    p.add_argument("--workspace", "-w", type=Path, default=Path.cwd(),
                   help="Workspace root for the TaskRunner gateway (default: cwd).")
    p.add_argument("--no-ui", action="store_true",
                   help="Headless mode: don't auto-open the Electron app; terminal-only fallback.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="DEBUG-level logging to stderr.")
    p.add_argument("--voice", action="store_true",
                   help="Enable voice channel (TTS + STT). Requires yuyutsava[voice] extras "
                        "and PIPER_MODEL / STT_PROVIDER env vars.")
    return p


async def _run_uvicorn(server: uvicorn.Server, stop_event: asyncio.Event) -> None:
    """Run uvicorn alongside the asyncio loop and stop it on shutdown."""
    serve_task = asyncio.create_task(server.serve(), name="uvicorn-serve")
    try:
        await stop_event.wait()
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except asyncio.TimeoutError:
            serve_task.cancel()
            try:
                await serve_task
            except (asyncio.CancelledError, Exception):
                pass


async def _open_electron_when_ready(url: str) -> None:
    await asyncio.sleep(0.4)
    electron_app_dir = Path(__file__).resolve().parent.parent.parent / "electron-app"
    if electron_app_dir.is_dir():
        try:
            subprocess.Popen(
                ["npm", "run", "dev"],
                cwd=electron_app_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            logger.warning("Could not launch Electron app; visit %s manually", url)
    else:
        logger.warning("Electron app not found at %s; visit %s manually", electron_app_dir, url)


async def _async_main(argv: list[str] | None = None) -> int:
    if load_dotenv:
        load_dotenv()

    args = _build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    workspace = args.workspace.resolve()
    home = yuyutsava_home()

    daemon_cfg = DaemonConfig.from_env()
    events_cfg = EventsConfig.from_file()

    # --voice flag enables the voice event source if not already in config.
    if getattr(args, "voice", False) and "voice" not in events_cfg.sources:
        from yuyutsava.core.config import SourceConfig
        events_cfg = EventsConfig(sources={
            **events_cfg.sources,
            "voice": SourceConfig(name="voice", enabled=True, params={}),
        })

    # Inject heartbeat_sec into every source's params so sources can sleep
    # between bursts rather than spinning. Sources ignore unknown params.
    if daemon_cfg.heartbeat_sec > 0:
        from yuyutsava.core.config import SourceConfig
        events_cfg = EventsConfig(sources={
            name: SourceConfig(
                name=src.name,
                enabled=src.enabled,
                params={**src.params, "heartbeat_sec": daemon_cfg.heartbeat_sec},
            )
            for name, src in events_cfg.sources.items()
        })

    # ── store --------------------------------------------------------------
    store = Store()
    await store.start()

    # ── user prefs store + orchestrator injector --------------------------
    prefs_store = UserPrefsStore(store)
    prefs_injector = PrefsInjector(prefs_store)

    # Re-apply logging with any persisted runtime level (CLI --verbose wins).
    persisted_level = prefs_store.get("daemon.log_level", None)
    if not args.verbose and isinstance(persisted_level, str):
        _setup_logging(args.verbose, persisted_level)

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

    # ── checkpointer (SQLite-backed, sweeps stale threads on a schedule) --
    checkpointer_mgr = CheckpointerManager(db_path=home / "checkpoints.db")
    checkpointer = await checkpointer_mgr.start()

    # ── blob sweeper (on-disk TTL for source-produced JPEGs/clips) --------
    # Webcam frames pile up fast (potentially one every few seconds for
    # hours). Keep ~1h of history then delete files + matching
    # event_payloads rows. Enrolled-faces DB at ~/.yuyutsava/deepface/ is
    # in a sibling directory and is NEVER swept — that's user data.
    blob_sweeper = BlobSweeper(
        store=store,
        targets=[
            BlobSweepTarget(
                name="webcam",
                directory=home / "blobs" / "webcam",
                ttl_sec=3600,
                glob="*.jpg",
            ),
        ],
    )
    await blob_sweeper.start()

    # ── events sweeper (TTL for non-blob event_payloads rows) ------------
    events_sweeper = EventsSweeper(store=store)
    await events_sweeper.start()

    # ── bus ---------------------------------------------------------------
    bus = EventBus()

    # ── sources -----------------------------------------------------------
    registry = SourceRegistry(bus, store, events_cfg)
    await registry.start_all()

    # ── channels ----------------------------------------------------------
    channels = ChannelRouter(channels=[], primary_name="web")
    web_hub: WebHub | None = None
    web_server: uvicorn.Server | None = None

    if not args.no_ui:
        web_hub = WebHub(store)
        channels.channels.append(WebChannel(web_hub))

    # Always include terminal as a fallback (and only channel in --no-ui mode).
    channels.channels.append(TerminalChannel(verbose=args.verbose))
    if args.no_ui:
        channels.primary_name = "terminal"

    # Voice channel — optional, disabled by default (privacy).
    if args.voice:
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

    stop_event = asyncio.Event()
    reload_event = asyncio.Event()
    install_signal_handlers(stop_event)
    install_reload_handler(reload_event)

    async def _hot_reload_events_config() -> None:
        """Re-read events_config.json and rebind sources in place."""
        new_cfg = EventsConfig.from_file()
        # Reapply the heartbeat_sec inject so live sources still sleep.
        if daemon_cfg.heartbeat_sec > 0:
            from yuyutsava.core.config import SourceConfig
            new_cfg = EventsConfig(sources={
                name: SourceConfig(
                    name=src.name,
                    enabled=src.enabled,
                    params={**src.params, "heartbeat_sec": daemon_cfg.heartbeat_sec},
                )
                for name, src in new_cfg.sources.items()
            })
        await registry.reload(new_cfg)
        logger.info("config reload: events sources now %s",
                    ", ".join(new_cfg.sources.keys()) or "(none)")

    # ── web server -------------------------------------------------------
    server_task: asyncio.Task[None] | None = None
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

    async def _reload_loop() -> None:
        """On SIGHUP: re-read MCP + events configs and hot-reload."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(reload_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            reload_event.clear()
            if stop_event.is_set():
                return
            try:
                new_mcp = MCPConfig.from_file()
                await mcp_manager.hot_reload(new_mcp)
                logger.info("config reload: mcp servers now %s",
                            ", ".join(mcp_manager.known_servers()) or "(none)")
            except Exception:
                logger.exception("mcp config reload failed")
            try:
                await _hot_reload_events_config()
            except Exception:
                logger.exception("events config reload failed")

    url = f"http://{daemon_cfg.web_host}:{daemon_cfg.web_port}/"
    logger.info("YUYUTSAVA daemon ready")
    logger.info("  workspace : %s", workspace)
    logger.info("  home      : %s", home)
    logger.info("  heartbeat : %ss", daemon_cfg.heartbeat_sec if daemon_cfg.heartbeat_sec > 0 else "disabled")
    logger.info("  triage    : %s / %s", triage_settings.__class__.__name__, triage_settings.model)
    logger.info("  orch      : %s / %s", orchestrator_settings.__class__.__name__, orchestrator_settings.model)
    logger.info("  subagents : %s", ", ".join(subagents.keys()))
    if web_hub is not None:
        logger.info("  web window: %s", url)

    if web_server is not None and not args.no_ui:
        asyncio.create_task(_open_electron_when_ready(url))

    # ── concurrent loops --------------------------------------------------
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(triage_loop.run(stop_event), name="triage-loop"),
        asyncio.create_task(orch_loop.run(stop_event), name="orchestrator-loop"),
        asyncio.create_task(_reload_loop(), name="reload-loop"),
    ]
    if web_server is not None:
        tasks.append(asyncio.create_task(_run_uvicorn(web_server, stop_event), name="web-server"))

    try:
        # Wait for stop_event; if any task crashes, also stop.
        done, pending = await asyncio.wait(
            [asyncio.create_task(stop_event.wait(), name="stop-wait"), *tasks],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            if t.get_name() != "stop-wait":
                exc = t.exception() if not t.cancelled() else None
                if exc:
                    logger.exception("loop crashed: %s", t.get_name(), exc_info=exc)
        stop_event.set()
        # Stop sources first so no new events arrive…
        await registry.stop_all()
        # …then close the bus to wake the triage loop's async-for…
        await bus.close()
        # …then drain in-flight tasks.
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.warning("shutdown drain timed out; cancelling")
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        logger.info("shutting down…")
        await channels.shutdown()
        await mcp_manager.stop()
        await blob_sweeper.stop()
        await events_sweeper.stop()
        await checkpointer_mgr.stop()
        await store.stop()
        logger.info("bye")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_async_main(argv))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
