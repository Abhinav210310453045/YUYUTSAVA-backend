"""
Daemon entry point — lifecycle only.

Subsystem construction lives in :mod:`yuyutsava.daemon.bootstrap`. This
file owns:
    - logging setup (the command line itself is :mod:`yuyutsava.daemon.options`)
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

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import uvicorn

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

from yuyutsava import platform as yplatform
from yuyutsava.aio import run as aio_run
from yuyutsava.aio.tasks import cancel_and_reap
from yuyutsava.async_subagents.host_lock import release_host_lock
from yuyutsava.config.dependencies import check_install
from yuyutsava.config.env import env_flag
from yuyutsava.config.logs import quiet_grpc_fork_logs, silence_plumbing_loggers
from yuyutsava.daemon.bootstrap import DaemonOptions, DaemonSubsystems, build_daemon
from yuyutsava.daemon.lifecycle import install_reload_handler, install_signal_handlers
from yuyutsava.daemon.options import build_parser
from yuyutsava.daemon.orchestrator_loop import resume_interrupted_tasks
from yuyutsava.daemon.setup_mode import models_problem, run_setup_mode
from yuyutsava.daemon.singleton import (
    acquire_daemon_lock,
    read_daemon_discovery,
    register_daemon_cleanup,
    release_daemon_lock,
    write_daemon_discovery,
)
from yuyutsava.daemon.wake_bridge import run_wake_bridge
from yuyutsava.mcp.config import MCPConfig
from yuyutsava.storage.paths import ensure_state_dirs, state_dir

# After the imports and before any model is built — see the function, which
# explains why that window is the whole requirement.
quiet_grpc_fork_logs()

logger = logging.getLogger("yuyutsava.daemon")

# _async_main returns this to main() to ask for a graceful self-reexec after
# ordered teardown (config hot-reload for standalone/CLI daemons).
_REEXEC_RETCODE = 75


_LOG_LEVEL_NAMES = ("DEBUG", "INFO", "WARNING")


def _resolve_level(name: str | None, fallback: int) -> int:
    if not name:
        return fallback
    upper = name.upper()
    if upper not in _LOG_LEVEL_NAMES:
        return fallback
    return getattr(logging, upper)


def _setup_logging(
    verbose: bool,
    persisted_level: str | None = None,
    *,
    debug_plumbing: bool = False,
) -> None:
    """Configure the daemon's ``yuyutsava`` logger.

    ``--verbose`` forces DEBUG on the ``yuyutsava`` namespace; otherwise the
    persisted pref applies, defaulting to INFO. The root logger stays at
    WARNING so unrelated libraries can't flood stderr. Plumbing loggers
    (uvicorn / langgraph runtime / httpx) are silenced unconditionally;
    pass ``debug_plumbing=True`` (or set ``YUYUTSAVA_DEBUG_PLUMBING=1``)
    to see them.
    """
    if not debug_plumbing:
        debug_plumbing = env_flag("YUYUTSAVA_DEBUG_PLUMBING", true=("1", "true", "yes"))

    if verbose:
        level = logging.DEBUG
    else:
        level = _resolve_level(persisted_level, logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    )

    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level < logging.WARNING:
        root.setLevel(logging.WARNING)

    log = logging.getLogger("yuyutsava")
    log.setLevel(level)
    log.handlers.clear()
    log.addHandler(handler)
    log.propagate = False

    if not debug_plumbing:
        silence_plumbing_loggers()


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
            await cancel_and_reap(serve_task, what="web server")


async def _open_electron_when_ready(
    url: str,
    holder: dict[str, subprocess.Popen] | None = None,
) -> None:
    await asyncio.sleep(0.4)
    electron_app_dir = Path(__file__).resolve().parent.parent.parent / "electron-app"
    if electron_app_dir.is_dir():
        try:
            # Off-loop: Popen's fork/exec handshake uses os.read/os.write, which
            # blockbuster flags on the event loop under allow_blocking=False.
            #
            # spawn_detached gives the UI subtree stdin=DEVNULL (vite sees no TTY,
            # so it never puts the user's terminal into raw mode) and its own
            # session/process group (new session on POSIX, new process group on
            # Windows), off our terminal — so killing the daemon can't strand
            # terminal-attached children. Because a Ctrl+C on the daemon then does
            # NOT reach the UI subtree, we keep the handle and tear the whole tree
            # down explicitly on shutdown (see _shutdown_ui via kill_tree).
            # Otherwise an orphaned vite keeps holding port 5173 and the next
            # `yuyutsava daemon` races a stale dev server → the UI fails to open.
            proc = await asyncio.to_thread(
                yplatform.spawn_detached,
                ["npm", "run", "dev"],
                cwd=electron_app_dir,
            )
            if holder is not None:
                holder["proc"] = proc
            return
        except OSError as exc:
            logger.warning("Could not launch Electron app (%s); visit %s manually", exc, url)
    else:
        logger.warning("Electron app not found at %s; visit %s manually", electron_app_dir, url)


def _shutdown_ui(holder: dict[str, subprocess.Popen]) -> None:
    """Tear down the detached UI subtree (vite + electron).

    The UI was launched via ``yplatform.spawn_detached`` (own session/process
    group), so ``kill_tree`` reaps every descendant in one shot. Without this
    the UI is orphaned on daemon stop, leaving vite holding port 5173 and
    breaking the next launch.
    """
    proc = holder.get("proc")
    if proc is None or proc.poll() is not None:
        return
    # spawn_detached gave the UI its own session/process group (POSIX) or
    # process group (Windows); kill_tree reaps the whole subtree (vite + electron).
    try:
        yplatform.kill_tree(proc.pid, timeout=5.0)
    except Exception:
        logger.exception("failed to tear down UI subprocess")


async def _run_memory_backfill(subs: DaemonSubsystems) -> None:
    """One-shot at startup: probe the embedder, then re-embed memory rows left
    vectorless by a prior embedder outage so they rejoin vector search.

    No-op unless the pgvector store is active (the SQLite twin has no
    embeddings). The probe gives one clear warning if the embedder is down
    (instead of a storm of per-operation tracebacks) and skips the backfill so
    we don't hammer a dead endpoint. Never fatal — memory is an enhancement.
    """
    # No getattr probe: every memory store declares backfill_embeddings since
    # ADR-002 step 2.5b. `memory_store` is still None when memory is disabled.
    if subs.memory_store is None or subs.embedder is None:
        return
    if not await subs.embedder.healthcheck():
        return  # warning already logged by healthcheck
    try:
        await subs.memory_store.backfill_embeddings()
    except Exception:
        logger.exception("memory: startup backfill failed")


async def _reload_loop(
    subs: DaemonSubsystems,
    stop_event: asyncio.Event,
    reload_event: asyncio.Event,
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
            new_mcp = MCPConfig.load(subs.workspace)
            await subs.mcp_manager.hot_reload(new_mcp)
            logger.info(
                "config reload: mcp servers now %s",
                ", ".join(s.summary() for s in subs.mcp_manager.status()) or "(none)",
            )
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
    logger.info(
        "  heartbeat : %ss",
        subs.daemon_cfg.heartbeat_sec if subs.daemon_cfg.heartbeat_sec > 0 else "disabled",
    )
    logger.info("  triage    : %s / %s", subs.triage_settings.type, subs.triage_settings.model)
    logger.info(
        "  orch      : %s / %s",
        subs.orchestrator_settings.type,
        subs.orchestrator_settings.model,
    )
    logger.info("  subagents : %s", ", ".join(subs.subagent_names))
    if subs.web_hub is not None:
        logger.info("  web window: %s", subs.web_url)
    _log_install_check()


def _log_install_check() -> None:
    """The coupled dependencies' versions, and a warning when they drift.

    An install that ignores ``uv.lock`` runs code the contracts and CI never
    checked — how the MCP servers broke (``config/dependencies.py``).
    """
    try:
        report = check_install()
    except Exception:  # noqa: BLE001 — a failed check must not stop the daemon starting
        logger.debug("dependency check failed", exc_info=True)
        return
    logger.info("  deps      : %s", ", ".join(f"{n} {v}" for n, v in report.versions.items()))
    if report.drifted:
        logger.warning(
            "this install does not match the project's locked dependencies: %s. "
            "Reinstall with `python scripts/install_cli.py` (README → Installing the CLI), "
            "or run the daemon with `uv run yuyutsava daemon` from the checkout.",
            "; ".join(report.out_of_range + report.off_lock),
        )


def _load_env() -> None:
    """Load the project ``.env``, then the app-managed one over it."""
    if not load_dotenv:
        return
    load_dotenv()  # project-dir .env (developer defaults)
    # ~/.yuyutsava/.env is the app-managed config the Settings UI writes;
    # it is authoritative, so load it last with override=True. This is what
    # lets a self-reexec reload (which inherits the previous process's stale
    # environment) pick up freshly-changed values.
    home_env = state_dir() / ".env"
    if home_env.is_file():
        load_dotenv(home_env, override=True)


def _acquire_singleton() -> int | None:
    """The daemon lock for this user profile, or ``None`` if one is running.

    On ``None`` the running daemon has already been described in the log.
    """
    lock_fd = acquire_daemon_lock()
    if lock_fd is not None:
        register_daemon_cleanup(lock_fd)
        return lock_fd
    disco = read_daemon_discovery()
    if disco:
        logger.warning(
            "yuyutsava daemon already running:\n"
            "  pid       = %s\n"
            "  web_url   = %s\n"
            "  async_host= %s\n"
            "Stop it with: yuyutsava daemon --stop",
            disco.get("pid"),
            disco.get("web_url"),
            disco.get("async_host_url") or "(disabled)",
        )
    else:
        logger.warning(
            "yuyutsava daemon lockfile is held but discovery is missing.\n"
            "If this persists, remove ~/.yuyutsava/daemon.lock manually."
        )
    return None


async def _publish_discovery(subs: DaemonSubsystems) -> None:
    """Write the discovery file other processes find this daemon by."""
    async_host_url = None
    if subs.async_host_attachment is not None:
        async_host_url = getattr(subs.async_host_attachment, "url", None)
    # Off-loop: write_daemon_discovery does os.replace, which blockbuster flags
    # when run on the event loop (allow_blocking=False).
    await asyncio.to_thread(
        write_daemon_discovery,
        pid=os.getpid(),
        web_url=subs.web_url,
        async_host_url=async_host_url,
    )


def _start_loops(
    subs: DaemonSubsystems, stop_event: asyncio.Event, reload_event: asyncio.Event
) -> list[asyncio.Task[None]]:
    """Start every long-running loop; the daemon stops when any of them ends."""
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(subs.triage_loop.run(stop_event), name="triage-loop"),
        asyncio.create_task(subs.orch_loop.run(stop_event), name="orchestrator-loop"),
        asyncio.create_task(subs.sweeper.run(stop_event), name="unified-sweeper"),
        asyncio.create_task(subs.resource_monitor.run(stop_event), name="resource-monitor"),
        asyncio.create_task(_reload_loop(subs, stop_event, reload_event), name="reload-loop"),
    ]
    if subs.web_server is not None:
        tasks.append(
            asyncio.create_task(
                _run_uvicorn(subs.web_server, stop_event),
                name="web-server",
            )
        )
    if subs.web_hub is not None:
        # Relay wake-word detections to the UI so it can open the voice overlay.
        tasks.append(
            asyncio.create_task(
                run_wake_bridge(
                    subs.bus,
                    subs.web_hub,
                    stop_event,
                    runtime_settings=subs.runtime_settings,
                ),
                name="wake-bridge",
            )
        )
    return tasks


async def _run_until_stopped(
    subs: DaemonSubsystems, tasks: list[asyncio.Task[None]], stop_event: asyncio.Event
) -> None:
    """Wait for a stop (or any loop to end), then drain the loops in order."""
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


async def _teardown(
    subs: DaemonSubsystems, ui_holder: dict[str, subprocess.Popen], lock_fd: int
) -> None:
    """Release everything the daemon holds, in dependency order."""
    logger.info("shutting down…")
    # Tear down the UI subtree we spawned (vite + electron). It runs in its
    # own session, so it survives our SIGINT unless we kill its group here —
    # otherwise an orphaned vite holds port 5173 and the next daemon start
    # races a stale dev server, so the UI fails to open.
    _shutdown_ui(ui_holder)
    # Async subagents teardown: watcher first (cancels in-flight runs on
    # the lg server via SDK + marks mirror entries cancelled), then the
    # host attachment (unlinks the host lockfile + discovery if we were
    # the owner; no-op for an attacher).
    if subs.async_task_watcher is not None:
        try:
            await subs.async_task_watcher.shutdown()
        except Exception:
            logger.exception("async_task_watcher.shutdown failed")
    if subs.async_host_attachment is not None:
        try:
            release_host_lock(subs.async_host_attachment)
        except Exception:
            logger.exception("release_host_lock failed")
    # The log bridge goes early: it holds a handler on the `yuyutsava`
    # logger, and everything below here logs while shutting down. Leaving
    # it attached would have those records queued for a hub that is about
    # to disappear.
    if getattr(subs, "log_bridge", None) is not None:
        try:
            await subs.log_bridge.aclose()
        except Exception:
            logger.exception("log_bridge.aclose failed")
    # Channel plugins first: their inbound pollers post through the
    # router, so they must stop before the channels they fan out to.
    try:
        await subs.channel_plugins.stop_all()
    except Exception:
        logger.exception("channel_plugins.stop_all failed")
    await subs.channels.shutdown()
    await subs.mcp_manager.stop()
    # The models config watcher's thread; joining it blocks briefly, so off the loop.
    await asyncio.to_thread(subs.config_watcher.stop)
    # Sweeper task is joined by the drain (it is one of the loops); closing
    # the saver here releases the checkpoints.db lock.
    await subs.checkpointer_saver.stop()
    await subs.store.stop()
    # Stop the storage health probe before the pool closes (it polls the pool).
    if subs.storage_health is not None:
        try:
            await subs.storage_health.stop()
        except Exception:
            logger.exception("storage_health.stop failed")
    # Postgres-backed runs: embedder's httpx client, then the pool —
    # last, because every pg store borrows connections from it.
    if subs.embedder is not None:
        try:
            await subs.embedder.aclose()
        except Exception:
            logger.exception("embedder.aclose failed")
    if subs.pg_pool is not None:
        try:
            await subs.pg_pool.close()
        except Exception:
            logger.exception("pg_pool.close failed")
    # Release the daemon singleton lock + discovery file.
    release_daemon_lock(lock_fd)
    logger.info("bye")


async def _async_main(argv: list[str] | None = None) -> int:
    _load_env()
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, debug_plumbing=args.debug_plumbing)

    # Singleton: refuse to start a second daemon for this user profile.
    lock_fd = _acquire_singleton()
    if lock_fd is None:
        return 0

    # No model it can use (a fresh install, a login that stopped working): wait
    # to be set up instead of failing to boot, then come back as the full daemon.
    problem = await asyncio.to_thread(models_problem)
    if problem is not None:
        return _REEXEC_RETCODE if await run_setup_mode(problem, lock_fd=lock_fd) else 0

    opts = DaemonOptions(
        workspace=args.workspace,
        headless=args.no_ui,
        voice=args.voice,
        verbose=args.verbose,
    )
    subs = await build_daemon(opts)
    await _publish_discovery(subs)

    # Re-apply logging with any persisted runtime level (CLI --verbose wins).
    # prefs_store.get is a synchronous sqlite read → off-loop.
    persisted_level = await asyncio.to_thread(subs.prefs_store.get, "daemon.log_level", None)
    if not args.verbose and isinstance(persisted_level, str):
        _setup_logging(args.verbose, persisted_level, debug_plumbing=args.debug_plumbing)

    stop_event = asyncio.Event()
    reload_event = asyncio.Event()
    reexec_event = asyncio.Event()
    install_signal_handlers(stop_event)
    install_reload_handler(reload_event)

    # Let POST /system/reload trigger a graceful self-reexec (for standalone /
    # CLI daemons not managed by the Electron app, which restarts via its own
    # supervisor). Setting reexec + stop makes _async_main return _REEXEC_RETCODE
    # after ordered teardown; main() then re-execs to pick up new config.
    if subs.web_server is not None:
        _web_app = subs.web_server.config.app
        _web_app.state.lifecycle_stop = stop_event
        _web_app.state.lifecycle_reexec = reexec_event

    _log_ready_banner(subs)

    ui_holder: dict[str, subprocess.Popen] = {}
    if subs.web_server is not None and subs.web_url and not (args.no_ui or args.no_app):
        asyncio.create_task(_open_electron_when_ready(subs.web_url, ui_holder))

    # Detached one-shot: rescue any memories left vectorless by an earlier
    # embedder outage. Kept out of the loops' wait-set — it completes quickly
    # and must not trip the FIRST_COMPLETED shutdown. The reference is held
    # for the lifetime of this function so the task isn't GC'd mid-run.
    _backfill_task = asyncio.create_task(  # noqa: F841 — anchor against GC
        _run_memory_backfill(subs),
        name="memory-backfill",
    )

    tasks = _start_loops(subs, stop_event, reload_event)

    # Durable resume: re-enqueue tasks a previous instance left unfinished
    # (e.g. after a config hot-reload restart). The orchestrator loop above is
    # already draining the queue; ``running`` tasks continue from their last
    # checkpoint, ``queued`` tasks re-run fresh.
    resumed = await resume_interrupted_tasks(subs.task_registry, subs.task_queue)
    if resumed:
        logger.info("startup: resumed %d task(s) from a previous run", resumed)

    try:
        await _run_until_stopped(subs, tasks, stop_event)
    finally:
        await _teardown(subs, ui_holder, lock_fd)
    # When a reload was requested, ask main() to re-exec now that the lock and
    # all resources are released, so the fresh process can re-acquire cleanly.
    return _REEXEC_RETCODE if reexec_event.is_set() else 0


def main(argv: list[str] | None = None) -> int:
    ensure_state_dirs()
    # --status and --stop are answered by the CLI before the daemon is even
    # imported (cli/commands/daemon_ctl.py); arriving here, they are a mistake.
    parser = build_parser()
    pre_args, _ = parser.parse_known_args(argv)
    if pre_args.status or pre_args.stop:
        parser.error("--status and --stop are handled by `yuyutsava daemon`")
    try:
        # aio.run installs the Selector loop policy on Windows (psycopg needs
        # it); on POSIX it is a passthrough to asyncio.run.
        rc = aio_run(_async_main(argv))
    except KeyboardInterrupt:
        return 0
    if rc == _REEXEC_RETCODE:
        # Graceful self-reexec to apply a config reload. The singleton lock was
        # released during teardown, so the replacement image re-acquires it.
        logger.info("re-executing daemon to apply config reload")
        os.execv(sys.executable, [sys.executable, *sys.argv])
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
