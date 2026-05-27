"""In-process LangGraph Agent Protocol host for async subagents.

The master deepagent's ``AsyncSubAgent`` entries point at the URL of an
Agent Protocol server. For *local* subagents whose graph code lives in this
process, we host that server inside the same process via a daemon thread
running ``langgraph_api.cli.run_server`` with ``runtime_edition='inmem'``.

From the user's perspective the YUYUTSAVA daemon is one process: the existing
FastAPI on its user-facing port keeps serving the Electron renderer, and this
LangGraph server lives on an internal loopback port that's not user-visible.

Design highlights
-----------------
* ``run_server`` accepts ``graphs={...}`` as a dict but serialises it to the
  ``LANGSERVE_GRAPHS`` env var as JSON — values must be ``"module:variable"``
  strings, not compiled graph objects. We bridge that via
  :mod:`yuyutsava.async_subagents._lg_graphs`, which exposes compiled graphs
  as module attributes resolved on demand.
* The server runs in a ``threading.Thread(daemon=True)`` — uvicorn owns its
  own asyncio loop; the daemon's main loop is untouched.
* Shutdown is best-effort: the uvicorn ``Server`` instance is held inside
  ``run_server``'s closure and is not externally addressable. Daemon-thread
  semantics ensure the worker dies when the process exits. For tests that need
  explicit cleanup, the caller can ``os._exit`` or rely on pytest teardown.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from typing import Iterable

from yuyutsava.async_subagents import _lg_graphs

logger = logging.getLogger("yuyutsava.async_subagents.host")


def _pick_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AsyncSubagentHost:
    """Owns a background ``langgraph_api`` server for local async subagents.

    Usage::

        host = AsyncSubagentHost.from_graphs({
            "file-organizer":  organizer_subagent.build_async_graph(model, ck),
            "general-purpose": general_subagent.build_async_graph(model, ck),
        })
        host.start()                  # blocks until /ok responds
        url = host.url                # "http://127.0.0.1:<port>"

        # ... pass url into AsyncSubAgent(url=url, graph_id="file-organizer")

        host.shutdown()               # best-effort
    """

    def __init__(
        self,
        *,
        graphs: dict[str, object],
        host: str = "127.0.0.1",
        port: int | None = None,
        healthcheck_timeout_sec: float = 60.0,
        server_log_level: str = "WARNING",
        allow_blocking: bool = True,
    ) -> None:
        if not graphs:
            raise ValueError("AsyncSubagentHost requires at least one graph")
        if any(not gid for gid in graphs):
            raise ValueError("AsyncSubagentHost: graph_id must be a non-empty string")
        self._graphs = dict(graphs)
        self._host = host
        self._port = port or _pick_free_port()
        self._healthcheck_timeout = healthcheck_timeout_sec
        self._server_log_level = server_log_level
        self._allow_blocking = allow_blocking
        self._thread: threading.Thread | None = None
        self._started = False

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @property
    def port(self) -> int:
        return self._port

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def graph_ids(self) -> list[str]:
        return list(self._graphs)

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_subagents(
        cls,
        subagents: Iterable,
        *,
        model,
        checkpointer,
        **kwargs,
    ) -> "AsyncSubagentHost":
        """Build a host from a sequence of ``BaseSubAgent`` instances.

        Each subagent's ``build_async_graph(model, checkpointer)`` is compiled
        once and registered under ``subagent.async_graph_id()``.
        """
        graphs: dict[str, object] = {}
        for sa in subagents:
            if not getattr(sa, "supports_async", False):
                continue
            graphs[sa.async_graph_id()] = sa.build_async_graph(model, checkpointer)
        return cls(graphs=graphs, **kwargs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return

        # Stash compiled graphs in the importable module before the langgraph
        # loader tries to resolve them.
        graphs_param: dict[str, str] = {}
        for graph_id, graph in self._graphs.items():
            attr = _lg_graphs.register(graph_id, graph)
            graphs_param[graph_id] = f"yuyutsava.async_subagents._lg_graphs:{attr}"

        def _serve() -> None:
            try:
                from langgraph_api.cli import run_server  # local import: heavy
                run_server(
                    host=self._host,
                    port=self._port,
                    graphs=graphs_param,
                    runtime_edition="inmem",
                    reload=False,
                    open_browser=False,
                    server_level=self._server_log_level,
                    allow_blocking=self._allow_blocking,
                )
            except Exception:  # pragma: no cover  # uvicorn exit / shutdown
                logger.exception("AsyncSubagentHost: server thread exited with error")

        self._thread = threading.Thread(target=_serve, name="async-subagent-host", daemon=True)
        self._thread.start()
        self._started = True

        if not self._wait_for_health():
            raise RuntimeError(
                f"AsyncSubagentHost: /ok healthcheck failed within "
                f"{self._healthcheck_timeout:.0f}s on {self.url}"
            )
        logger.info("AsyncSubagentHost ready on %s (graphs=%s)", self.url, sorted(self._graphs))

    def shutdown(self) -> None:
        """Best-effort teardown.

        Cannot directly stop uvicorn from the outside (no public handle), so we
        only deregister graph entries and rely on daemon-thread semantics for
        the worker to die on process exit. Callers that need a true clean stop
        before process exit should use ``os._exit`` or restart the process.
        """
        for graph_id in list(self._graphs):
            _lg_graphs.unregister(graph_id)
        self._started = False
        # Don't join — daemon thread; uvicorn ignores external signals.
        # Process exit will reap it.

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _wait_for_health(self) -> bool:
        deadline = time.time() + self._healthcheck_timeout
        url = f"{self.url}/ok"
        while time.time() < deadline:
            if not (self._thread and self._thread.is_alive()):
                return False
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        return True
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            time.sleep(0.3)
        return False
