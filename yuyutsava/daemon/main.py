"""
Daemon entry point — lifecycle only.

Subsystem construction lives in :mod:`yuyutsava.daemon.bootstrap`. This
file owns:
    - argparse + logging setup
    - bootstrap → run loops → ordered teardown
    - signal handlers + SIGHUP reload loop
    - Electron auto-launch + uvicorn co-execution

Boot order (delegated to ``bootstrap.build_daemon``):
    configs → store → prefs → policy → MCP → checkpointer → sweeper
        → bus → sources → channels → models → skills → subagents
        → triage agent + loop → orchestrator deps + loop → web server.

Shutdown order (here):
    sources → bus close → drain loops → channels → MCP → checkpointer
        → store.
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

from yuyutsava.daemon.bootstrap import DaemonOptions, DaemonSubsystems, build_daemon
from yuyutsava.daemon.lifecycle import install_reload_handler, install_signal_handlers
from yuyutsava.mcp.config import MCPConfig

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


async def _reload_loop(
    subs: DaemonSubsystems, stop_event: asyncio.Event, reload_event: asyncio.Event,
) -> None:
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
            await subs.mcp_manager.hot_reload(new_mcp)
            logger.info("config reload: mcp servers now %s",
                        ", ".join(subs.mcp_manager.known_servers()) or "(none)")
        except Exception:
            logger.exception("mcp config reload failed")
        try:
            await subs.hot_reload_events_config()
        except Exception:
            logger.exception("events config reload failed")


def _log_ready_banner(subs: DaemonSubsystems) -> None:
    logger.info("YUYUTSAVA daemon ready")
    logger.info("  workspace : %s", subs.workspace)
    logger.info("  home      : %s", subs.home)
    logger.info("  heartbeat : %ss",
                subs.daemon_cfg.heartbeat_sec if subs.daemon_cfg.heartbeat_sec > 0 else "disabled")
    logger.info("  triage    : %s / %s",
                subs.triage_settings.__class__.__name__, subs.triage_settings.model)
    logger.info("  orch      : %s / %s",
                subs.orchestrator_settings.__class__.__name__, subs.orchestrator_settings.model)
    logger.info("  subagents : %s", ", ".join(subs.subagent_names))
    if subs.web_hub is not None:
        logger.info("  web window: %s", subs.web_url)


async def _async_main(argv: list[str] | None = None) -> int:
    if load_dotenv:
        load_dotenv()

    args = _build_parser().parse_args(argv)
    _setup_logging(args.verbose)

    opts = DaemonOptions(
        workspace=args.workspace,
        headless=args.no_ui,
        voice=args.voice,
        verbose=args.verbose,
    )

    subs = await build_daemon(opts)

    # Re-apply logging with any persisted runtime level (CLI --verbose wins).
    persisted_level = subs.prefs_store.get("daemon.log_level", None)
    if not args.verbose and isinstance(persisted_level, str):
        _setup_logging(args.verbose, persisted_level)

    stop_event = asyncio.Event()
    reload_event = asyncio.Event()
    install_signal_handlers(stop_event)
    install_reload_handler(reload_event)

    _log_ready_banner(subs)

    if subs.web_server is not None and not args.no_ui:
        asyncio.create_task(_open_electron_when_ready(subs.web_url))

    # ── concurrent loops --------------------------------------------------
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(subs.triage_loop.run(stop_event), name="triage-loop"),
        asyncio.create_task(subs.orch_loop.run(stop_event), name="orchestrator-loop"),
        asyncio.create_task(subs.sweeper.run(stop_event), name="unified-sweeper"),
        asyncio.create_task(_reload_loop(subs, stop_event, reload_event), name="reload-loop"),
    ]
    if subs.web_server is not None:
        tasks.append(asyncio.create_task(
            _run_uvicorn(subs.web_server, stop_event), name="web-server",
        ))

    try:
        # Wait for stop_event; if any task crashes, also stop.
        done, _pending = await asyncio.wait(
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
        await subs.registry.stop_all()
        # …then close the bus to wake the triage loop's async-for…
        await subs.bus.close()
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
        await subs.channels.shutdown()
        await subs.mcp_manager.stop()
        # Sweeper task is joined via the gather() above (it's in `tasks`);
        # closing the saver here releases the checkpoints.db lock.
        await subs.checkpointer_saver.stop()
        await subs.store.stop()
        logger.info("bye")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_async_main(argv))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
