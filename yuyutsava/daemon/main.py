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
from yuyutsava.core.engine import silence_plumbing_loggers
from yuyutsava.daemon.bootstrap import DaemonOptions, DaemonSubsystems, build_daemon
from yuyutsava.daemon.lifecycle import install_reload_handler, install_signal_handlers
from yuyutsava.daemon.orchestrator_loop import resume_interrupted_tasks
from yuyutsava.daemon.singleton import (
    acquire_daemon_lock,
    read_daemon_discovery,
    register_daemon_cleanup,
    release_daemon_lock,
    write_daemon_discovery,
)
from yuyutsava.mcp.config import MCPConfig
from yuyutsava.storage.paths import ensure_state_dirs, state_dir

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
        debug_plumbing = os.environ.get("YUYUTSAVA_DEBUG_PLUMBING", "").lower() in ("1", "true", "yes")

    if verbose:
        level = logging.DEBUG
    else:
        level = _resolve_level(persisted_level, logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s",
                                           datefmt="%H:%M:%S"))

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
    p.add_argument("--debug-plumbing", action="store_true",
                   help=("Show uvicorn / langgraph runtime / httpx logs (debugging only). "
                         "Off by default. Also honoured via YUYUTSAVA_DEBUG_PLUMBING=1."))
    p.add_argument("--voice", action="store_true",
                   help="Enable voice channel (TTS + STT). Requires yuyutsava[voice] extras "
                        "and PIPER_MODEL / STT_PROVIDER env vars.")
    p.add_argument("--status", action="store_true",
                   help="Show the running daemon's PID, URLs, uptime; exit.")
    p.add_argument("--stop", action="store_true",
                   help="Send SIGTERM to the running daemon and wait for it to exit; exit.")
    return p


def _cmd_status() -> int:
    """Print info about the running daemon. Exits 0 if running, 1 otherwise."""
    disco = read_daemon_discovery()
    if disco is None:
        print("yuyutsava daemon: not running", file=sys.stderr)
        return 1
    pid = disco.get("pid")
    web_url = disco.get("web_url") or "(unknown)"
    async_host_url = disco.get("async_host_url") or "(disabled)"
    started_at = disco.get("started_at")
    uptime_str = "(unknown)"
    if isinstance(started_at, (int, float)):
        import time as _time
        secs = max(0, int(_time.time() - started_at))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        uptime_str = f"{h}h{m:02d}m{s:02d}s"
    print("yuyutsava daemon: running")
    print(f"  pid        = {pid}")
    print(f"  web_url    = {web_url}")
    print(f"  async_host = {async_host_url}")
    print(f"  uptime     = {uptime_str}")
    return 0


def _cmd_stop() -> int:
    """Stop the running daemon (graceful terminate, escalate to kill) — cross-platform."""
    from yuyutsava.daemon.singleton import daemon_lock_path, daemon_discovery_path

    def _cleanup_files() -> None:
        for p in (daemon_lock_path(), daemon_discovery_path()):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    disco = read_daemon_discovery()
    if disco is None:
        print("yuyutsava daemon: not running (nothing to stop)", file=sys.stderr)
        return 0
    pid = disco.get("pid")
    if not isinstance(pid, int):
        print("yuyutsava daemon: discovery file is malformed", file=sys.stderr)
        return 1

    if not yplatform.pid_alive(pid):
        print(f"yuyutsava daemon: pid {pid} not alive; cleaning up stale files",
              file=sys.stderr)
        _cleanup_files()
        return 0

    print(f"yuyutsava daemon: stopping pid {pid}…", file=sys.stderr)
    # terminate_pid sends SIGTERM (TerminateProcess on Windows), waits up to
    # 5s, then escalates to a hard kill. The daemon's atexit hook normally
    # unlinks the lock/discovery files; we sweep them best-effort regardless.
    ok = yplatform.terminate_pid(pid, timeout=5.0)
    _cleanup_files()
    print("yuyutsava daemon: stopped" if ok else
          "yuyutsava daemon: could not confirm stop (check for a stuck process)",
          file=sys.stderr)
    return 0 if ok else 1


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


async def _open_electron_when_ready(
    url: str, holder: dict[str, subprocess.Popen] | None = None,
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
        except Exception:
            logger.warning("Could not launch Electron app; visit %s manually", url)
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
    backfill = getattr(subs.memory_store, "backfill_embeddings", None)
    if backfill is None or subs.embedder is None:
        return
    if not await subs.embedder.healthcheck():
        return  # warning already logged by healthcheck
    try:
        await backfill()
    except Exception:
        logger.exception("memory: startup backfill failed")


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
        load_dotenv()  # project-dir .env (developer defaults)
        # ~/.yuyutsava/.env is the app-managed config the Settings UI writes;
        # it is authoritative, so load it last with override=True. This is what
        # lets a self-reexec reload (which inherits the previous process's stale
        # environment) pick up freshly-changed values.
        _home_env = state_dir() / ".env"
        if _home_env.is_file():
            load_dotenv(_home_env, override=True)

    args = _build_parser().parse_args(argv)
    _setup_logging(args.verbose, debug_plumbing=args.debug_plumbing)

    # Singleton: refuse to start a second daemon for this user profile.
    lock_fd = acquire_daemon_lock()
    if lock_fd is None:
        disco = read_daemon_discovery()
        if disco:
            print(
                "yuyutsava daemon already running:\n"
                f"  pid       = {disco.get('pid')}\n"
                f"  web_url   = {disco.get('web_url')}\n"
                f"  async_host= {disco.get('async_host_url') or '(disabled)'}\n"
                "Stop it with: yuyutsava daemon --stop",
                file=sys.stderr,
            )
        else:
            print(
                "yuyutsava daemon lockfile is held but discovery is missing.\n"
                "If this persists, remove ~/.yuyutsava/daemon.lock manually.",
                file=sys.stderr,
            )
        return 0
    register_daemon_cleanup(lock_fd)

    opts = DaemonOptions(
        workspace=args.workspace,
        headless=args.no_ui,
        voice=args.voice,
        verbose=args.verbose,
    )

    subs = await build_daemon(opts)
    # Publish discovery now that we know the URLs.
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
    if subs.web_server is not None and not args.no_ui:
        asyncio.create_task(_open_electron_when_ready(subs.web_url, ui_holder))

    # Detached one-shot: rescue any memories left vectorless by an earlier
    # embedder outage. Kept out of the ``tasks`` wait-set below — it completes
    # quickly and must not trip the FIRST_COMPLETED shutdown. The reference is
    # held for the lifetime of this function so the task isn't GC'd mid-run.
    _backfill_task = asyncio.create_task(  # noqa: F841 — anchor against GC
        _run_memory_backfill(subs), name="memory-backfill",
    )

    # ── concurrent loops --------------------------------------------------
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(subs.triage_loop.run(stop_event), name="triage-loop"),
        asyncio.create_task(subs.orch_loop.run(stop_event), name="orchestrator-loop"),
        asyncio.create_task(subs.sweeper.run(stop_event), name="unified-sweeper"),
        asyncio.create_task(subs.resource_monitor.run(stop_event), name="resource-monitor"),
        asyncio.create_task(_reload_loop(subs, stop_event, reload_event), name="reload-loop"),
    ]
    if subs.web_server is not None:
        tasks.append(asyncio.create_task(
            _run_uvicorn(subs.web_server, stop_event), name="web-server",
        ))
    if subs.web_hub is not None:
        # Relay wake-word detections to the UI so it can open the voice overlay.
        from yuyutsava.daemon.wake_bridge import run_wake_bridge
        tasks.append(asyncio.create_task(
            run_wake_bridge(subs.bus, subs.web_hub, stop_event), name="wake-bridge",
        ))

    # Durable resume: re-enqueue tasks a previous instance left unfinished
    # (e.g. after a config hot-reload restart). The orchestrator loop above is
    # already draining the queue; ``running`` tasks continue from their last
    # checkpoint, ``queued`` tasks re-run fresh.
    resumed = await resume_interrupted_tasks(subs.task_registry, subs.task_queue)
    if resumed:
        logger.info("startup: resumed %d task(s) from a previous run", resumed)

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
                from yuyutsava.async_subagents.host_lock import release_host_lock
                release_host_lock(subs.async_host_attachment)
            except Exception:
                logger.exception("release_host_lock failed")
        # Channel plugins first: their inbound pollers post through the
        # router, so they must stop before the channels they fan out to.
        try:
            await subs.channel_plugins.stop_all()
        except Exception:
            logger.exception("channel_plugins.stop_all failed")
        await subs.channels.shutdown()
        await subs.mcp_manager.stop()
        # Sweeper task is joined via the gather() above (it's in `tasks`);
        # closing the saver here releases the checkpoints.db lock.
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
    # When a reload was requested, ask main() to re-exec now that the lock and
    # all resources are released, so the fresh process can re-acquire cleanly.
    return _REEXEC_RETCODE if reexec_event.is_set() else 0


def main(argv: list[str] | None = None) -> int:
    ensure_state_dirs()
    # Parse once at the sync entry so --status / --stop don't pay the cost
    # of asyncio.run and don't try to acquire the daemon lock.
    pre_args, _ = _build_parser().parse_known_args(argv)
    if getattr(pre_args, "status", False):
        return _cmd_status()
    if getattr(pre_args, "stop", False):
        return _cmd_stop()
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
