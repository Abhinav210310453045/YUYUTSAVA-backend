"""A daemon with no model it can use waits to be set up, rather than dying at boot.

Before ``models.toml`` exists, the daemon reads its models from the environment;
with neither — a fresh install, or a login that no longer works — every
subsystem that needs a model would fail at boot, and the app would have no
daemon to show a setup screen through.

So the daemon first asks :func:`models_problem`. With a problem it serves only
the models API (``/models``, behind the same auth, on the same port, published
in the same discovery file), with ``setup_mode: true`` in its status so the app
opens its setup wizard. The moment the files describe models it can use — the
wizard wrote them, the CLI did, or someone edited them — it re-executes itself
as the full daemon, in the same process (``os.execv``), so the app's supervisor
never sees it go away.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import uvicorn

from yuyutsava.daemon.config import DaemonConfig
from yuyutsava.daemon.lifecycle import install_signal_handlers
from yuyutsava.daemon.singleton import release_daemon_lock, write_daemon_discovery
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.app_deps import AppDeps
from yuyutsava.daemon.web.auth import AuthSettings
from yuyutsava.daemon.web.services.models_service import ModelsService, broadcast_model_changes
from yuyutsava.daemon.web.services.stream_service import WebHub
from yuyutsava.llm.config.detect import detect
from yuyutsava.llm.config.store import ConfigStore
from yuyutsava.llm.config.watch import ConfigSource, ConfigWatcher
from yuyutsava.llm.failures import describe_model_failure
from yuyutsava.llm.providers.registry import discover
from yuyutsava.llm.selection import TaskModels
from yuyutsava.storage.paths import models_config_path, secrets_path

logger = logging.getLogger("yuyutsava.daemon.setup_mode")

#: The tasks whose models the daemon resolves or builds at boot.
_BOOT_TASKS = ("chat", "triage", "orchestrator", "subagent", "compaction")
_BUILT_AT_BOOT = ("orchestrator", "subagent", "compaction")


def models_problem() -> str | None:
    """Why the daemon could not run its models now, or ``None`` when it can.

    Resolves each task's model the way the boot does and builds the ones the boot
    builds — constructing a model sends nothing, so this is offline. Blocking
    (files, credential lookups): call it off the event loop.
    """
    source = ConfigSource(models_config_path(), secrets_path())
    state = source.reload()
    if state.problem is not None:
        return str(state.problem)
    models = TaskModels(source)
    try:
        for task in _BOOT_TASKS:
            models.target(task)
        for task in _BUILT_AT_BOOT:
            models.build(task, temperature=0.0)
    except Exception as exc:  # noqa: BLE001 — whatever stops a model being built is the answer
        return describe_model_failure(exc) or f"{type(exc).__name__}: {exc}"
    return None


class _NoEvents:
    """The events store a waiting daemon has: it records nothing."""

    async def put_event_payload(self, **_: Any) -> None: ...
    async def put_proposal(self, _proposal: Any) -> None: ...
    async def put_decision(self, **_: Any) -> None: ...


async def run_setup_mode(problem: str, *, lock_fd: int) -> bool:
    """Serve the models API until a usable config appears; ``True`` to re-exec as the full daemon.

    ``False`` when asked to stop first. Releases the singleton lock either way.
    """
    logger.warning("models: %s", problem)
    logger.warning("models: waiting to be set up — open the app, or run `yuyutsava models setup`")
    cfg = DaemonConfig.from_env()
    source = ConfigSource(models_config_path(), secrets_path())
    watcher = ConfigWatcher(source)
    await asyncio.to_thread(watcher.start)
    service = ModelsService(
        ConfigStore(models_config_path(), secrets_path()),
        source,
        discover=discover,
        detect=detect,
        setup_mode=True,
    )
    hub = WebHub(store=_NoEvents())
    broadcast_model_changes(source, service, hub)
    auth = await asyncio.to_thread(AuthSettings.from_env, host=cfg.web_host)
    app = create_app(hub, host=cfg.web_host, deps=AppDeps(models_service=service, auth=auth))
    server = uvicorn.Server(
        uvicorn.Config(app, host=cfg.web_host, port=cfg.web_port, log_level="warning")
    )

    loop = asyncio.get_running_loop()
    stop, usable = asyncio.Event(), asyncio.Event()

    def on_change(state: Any) -> None:
        # The watcher's thread: building the models to check them is fine here.
        if state.snapshot is not None and models_problem() is None:
            loop.call_soon_threadsafe(usable.set)

    source.subscribe(on_change)
    install_signal_handlers(stop)
    host = "127.0.0.1" if cfg.web_host in ("0.0.0.0", "::", "") else cfg.web_host
    await asyncio.to_thread(
        write_daemon_discovery,
        pid=os.getpid(),
        web_url=f"http://{host}:{cfg.web_port}/",
        async_host_url=None,
    )
    serving = asyncio.create_task(server.serve(), name="setup-mode-serve")
    waits = {
        serving,
        asyncio.create_task(stop.wait(), name="setup-mode-stop"),
        asyncio.create_task(usable.wait(), name="setup-mode-usable"),
    }
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        server.should_exit = True  # serve() returns once it has closed
        for task in waits - {serving}:
            task.cancel()
        await asyncio.gather(*waits, return_exceptions=True)
        await asyncio.to_thread(watcher.stop)
        release_daemon_lock(lock_fd)
    if usable.is_set():
        logger.info("models: set up — starting the full daemon")
    return usable.is_set()


__all__ = ["models_problem", "run_setup_mode"]
