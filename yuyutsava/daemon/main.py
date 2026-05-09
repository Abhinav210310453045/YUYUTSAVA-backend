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
import sys
import webbrowser
from pathlib import Path

import uvicorn

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

from yuyutsava.agents.file_organizer.agent import FileOrganizerAgent
from yuyutsava.agents.orchestrator.agent import OrchestratorDeps
from yuyutsava.agents.orchestrator.capabilities import render_capabilities_block
from yuyutsava.agents.task_runner.agent import TaskRunnerAgent
from yuyutsava.agents.triage.agent import TriageAgent
from yuyutsava.core.config import (
    DaemonConfig, EventsConfig, llm_settings_from_env, yuyutsava_home,
)
from yuyutsava.skills.registry import SkillRegistry
from yuyutsava.core.llm import chat_model
from yuyutsava.daemon.channels import ChannelRouter
from yuyutsava.daemon.lifecycle import install_signal_handlers
from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop
from yuyutsava.daemon.terminal_channel import TerminalChannel
from yuyutsava.daemon.triage_loop import OrchestratorTask, TriageLoop
from yuyutsava.daemon.web.server import WebChannel, WebHub, make_app
from yuyutsava.events.bus import EventBus
from yuyutsava.events.registry import SourceRegistry
from yuyutsava.events.store import Store

logger = logging.getLogger("yuyutsava.daemon")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s",
                                           datefmt="%H:%M:%S"))
    root = logging.getLogger("yuyutsava")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False
    # Quiet down access logs in the foreground.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yuyutsava daemon",
        description="Run the always-on YUYUTSAVA daemon.",
    )
    p.add_argument("--workspace", "-w", type=Path, default=Path.cwd(),
                   help="Workspace root for the TaskRunner gateway (default: cwd).")
    p.add_argument("--no-ui", action="store_true",
                   help="Headless mode: disable the web window. Terminal-only fallback.")
    p.add_argument("--no-browser", action="store_true",
                   help="Don't auto-open the browser; just print the URL.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="DEBUG-level logging to stderr.")
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


async def _open_browser_when_ready(url: str) -> None:
    # Tiny delay so the listener is up; webbrowser.open returns instantly.
    await asyncio.sleep(0.4)
    try:
        webbrowser.open(url)
    except Exception:
        logger.warning("Could not auto-open browser; visit %s manually", url)


async def _async_main(argv: list[str] | None = None) -> int:
    if load_dotenv:
        load_dotenv()

    args = _build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    workspace = args.workspace.resolve()
    home = yuyutsava_home()

    daemon_cfg = DaemonConfig.from_env()
    events_cfg = EventsConfig.from_file()
    if not events_cfg.sources:
        events_cfg = EventsConfig.default()
        logger.info("no events_config.json — using built-in default (watching ~/Downloads)")

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

    # ── subagents ---------------------------------------------------------
    task_runner = TaskRunnerAgent(workspace_root=workspace)
    subagent_list = [
        FileOrganizerAgent(task_runner, store, skill_registry=skill_registry),
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
    )
    orch_loop = OrchestratorLoop(
        task_queue=task_queue,
        channels=channels,
        store=store,
        orchestrator_model=orchestrator_model,
        deps=orch_deps,
        orchestrator_token_budget=daemon_cfg.orchestrator_token_budget,
    )

    # ── web server -------------------------------------------------------
    server_task: asyncio.Task[None] | None = None
    if web_hub is not None:
        app = make_app(web_hub, host=daemon_cfg.web_host, skill_registry=skill_registry)
        config = uvicorn.Config(
            app, host=daemon_cfg.web_host, port=daemon_cfg.web_port,
            log_level="warning", access_log=False, lifespan="on",
        )
        web_server = uvicorn.Server(config)

    stop_event = asyncio.Event()
    install_signal_handlers(stop_event)

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

    if web_server is not None and daemon_cfg.web_open_browser and not args.no_browser:
        asyncio.create_task(_open_browser_when_ready(url))

    # ── concurrent loops --------------------------------------------------
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(triage_loop.run(stop_event), name="triage-loop"),
        asyncio.create_task(orch_loop.run(stop_event), name="orchestrator-loop"),
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
