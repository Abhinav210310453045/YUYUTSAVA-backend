"""MCP server lifecycle: spawn/connect, list tools, scope, hot-reload, stop.

The :class:`MCPClientManager` is the single owner of every MCP client session.
The daemon creates and starts one in ``daemon/bootstrap.py`` after the store
and before the agents; the standalone CLI gets its own through
:func:`start_manager_for_workspace` (owned by the ``AgentBundle``, stopped in
``aclose``). Agents then call :meth:`tools_for` to get a list of ``BaseTool``
instances scoped to them.

Failure of one MCP server does not affect others: each server is tracked in an
``AsyncExitStack`` so partial shutdown stays clean.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.types import PaginatedRequestParams

from yuyutsava.mcp.config import MCPConfig, MCPServerSpec, global_mcp_config_path
from yuyutsava.mcp.status import CONNECTED, FAILED, NOT_RUNNING, ServerStatus, connectors_block
from yuyutsava.mcp.tool_adapter import adapt, sdk_field

logger = logging.getLogger("yuyutsava.mcp.loader")


@dataclass
class _ServerEntry:
    """Per-server runtime state.

    The transport + session ``AsyncExitStack`` is owned by ``runner`` and
    never touched from another task — anyio cancel scopes used inside the
    MCP SDK must be exited from the same task that entered them.
    """

    spec: MCPServerSpec
    session: ClientSession
    tools: list[BaseTool]
    runner: asyncio.Task[None]
    stop_event: asyncio.Event


class MCPClientManager:
    """Owns every MCP client session; serves tool lists per agent scope."""

    def __init__(self) -> None:
        self._servers: dict[str, _ServerEntry] = {}
        #: Why each server that failed to start failed, until it next starts.
        self._failures: dict[str, str] = {}
        self._config: MCPConfig = MCPConfig.empty()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, cfg: MCPConfig) -> None:
        """Spawn every server in *cfg*. Logs and skips servers that fail to start."""
        async with self._lock:
            self._config = cfg
            for name, spec in cfg.servers.items():
                await self._start_one(name, spec)

    async def stop(self) -> None:
        """Tear every server session down. Logs per-server progress."""
        async with self._lock:
            names = list(self._servers.keys())
            for name in names:
                await self._stop_one(name)
            self._servers.clear()

    async def hot_reload(self, new_cfg: MCPConfig) -> None:
        """Diff servers (added / removed / changed) and apply the delta.

        A server is "changed" if its spec is not equal to the running one;
        we stop and restart it. Running tasks that hold a reference to an
        old tool will see calls fail cleanly via the adapter's error path.
        """
        async with self._lock:
            old = self._config
            self._config = new_cfg

            removed = set(old.servers) - set(new_cfg.servers)
            added = set(new_cfg.servers) - set(old.servers)
            kept = set(old.servers) & set(new_cfg.servers)
            changed = {n for n in kept if old.servers[n] != new_cfg.servers[n]}

            for name in removed | changed:
                await self._stop_one(name)
            for name in added | changed:
                await self._start_one(name, new_cfg.servers[name])

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def tools_for(self, agent_name: str) -> list[BaseTool]:
        """Return tools scoped to *agent_name* per the config's ``scopes`` map.

        Missing agent → default_scope. Servers listed but not actually running
        (e.g., failed to start) contribute zero tools, not an error.
        """
        names = self._config.servers_for(agent_name)
        out: list[BaseTool] = []
        for n in names:
            entry = self._servers.get(n)
            if entry is None:
                logger.debug("scope %r references absent/dead MCP server %r", agent_name, n)
                continue
            out.extend(entry.tools)
        return out

    def known_servers(self) -> list[str]:
        return list(self._servers.keys())

    def status(self, agent_name: str | None = None) -> list[ServerStatus]:
        """Every configured server's state — or only those in *agent_name*'s scope."""
        names = (
            self._config.servers_for(agent_name)
            if agent_name is not None
            else list(self._config.servers)
        )
        out: list[ServerStatus] = []
        for name in names:
            spec = self._config.servers.get(name)
            if spec is None:
                continue
            entry = self._servers.get(name)
            if entry is not None:
                out.append(ServerStatus(name, CONNECTED, len(entry.tools), source=spec.source))
            elif name in self._failures:
                out.append(
                    ServerStatus(name, FAILED, error=self._failures[name], source=spec.source)
                )
            else:
                out.append(ServerStatus(name, NOT_RUNNING, source=spec.source))
        return out

    def connectors_block(self, agent_name: str) -> str:
        """The prompt lines that tell *agent_name* which connectors it has."""
        return connectors_block(self.status(agent_name), global_mcp_config_path())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _start_one(self, name: str, spec: MCPServerSpec) -> None:
        """Spawn the per-server runner task and wait until it has either
        registered itself or failed. The runner owns the transport stack for
        its entire lifetime so cancel-scope exit happens in the same task.
        """
        if name in self._servers:
            logger.debug("MCP server %r already running; skipping", name)
            return

        ready = asyncio.Event()
        stop_event = asyncio.Event()

        runner = asyncio.create_task(
            self._run_server(name, spec, ready, stop_event),
            name=f"mcp-server[{name}]",
        )
        # Wait for the runner to either register the entry or signal failure.
        ready_wait = asyncio.create_task(ready.wait())
        try:
            await asyncio.wait(
                {ready_wait, runner},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            ready_wait.cancel()

        if not ready.is_set():
            # Runner exited before signalling — it already logged. Surface
            # nothing; the manager simply skips this server.
            return

    async def _run_server(
        self,
        name: str,
        spec: MCPServerSpec,
        ready: asyncio.Event,
        stop_event: asyncio.Event,
    ) -> None:
        """Own the transport + session for one MCP server, end to end.

        Lives as a dedicated task so the AsyncExitStack opened here is also
        closed here — required by the anyio cancel scopes the MCP SDK uses.
        """
        async with AsyncExitStack() as stack:
            try:
                if spec.transport == "stdio":
                    params = StdioServerParameters(
                        command=spec.command,
                        args=list(spec.args),
                        env=dict(spec.env) if spec.env else None,
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                else:
                    read, write = await stack.enter_async_context(sse_client(spec.url))

                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                mcp_tools = await list_all_tools(session)
                adapted = [adapt(session, name, t, catalog_limit=spec.max_tools) for t in mcp_tools]
            except Exception as exc:  # noqa: BLE001 — one server must not stop the rest
                logger.exception("MCP server %r failed to start: %s", name, exc)
                self._failures[name] = _failure_text(exc)
                ready.set()
                return

            self._failures.pop(name, None)
            self._servers[name] = _ServerEntry(
                spec=spec,
                session=session,
                tools=adapted,
                runner=asyncio.current_task(),  # type: ignore[arg-type]
                stop_event=stop_event,
            )
            logger.info(
                "MCP server %r started — %d tool(s)%s",
                name,
                len(adapted),
                f" (from {spec.source})" if spec.source else "",
            )
            ready.set()

            # Park until shutdown is requested. The AsyncExitStack will
            # close on exit from this `async with`, in this same task.
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                logger.debug("MCP server %r runner cancelled", name)

    async def _stop_one(self, name: str) -> None:
        entry = self._servers.pop(name, None)
        if entry is None:
            return
        entry.stop_event.set()
        try:
            # `shield` keeps the runner's aclose alive even if the outer
            # shutdown task gets cancelled mid-wait.
            await asyncio.wait_for(asyncio.shield(entry.runner), timeout=3.0)
            logger.info("MCP server %r stopped", name)
        except asyncio.TimeoutError:
            logger.warning("MCP server %r stop timed out after 3s; cancelling runner", name)
            entry.runner.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(entry.runner), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        except Exception as exc:  # noqa: BLE001 — shutdown continues past one server
            logger.warning("MCP server %r stop raised %s", name, exc)


def _failure_text(exc: BaseException) -> str:
    """One line for the agent and the status endpoint: the innermost cause.

    The SDK wraps a server's own failure in anyio ``ExceptionGroup``s, whose
    message says only how many sub-exceptions there were.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = f"{type(exc).__name__}: {exc}".strip()
    return text if len(text) <= 200 else text[:199] + "…"


#: Pages of ``tools/list`` read from one server before giving up — a server
#: whose cursor never ends must not hang the start-up.
_MAX_TOOL_PAGES = 50


async def list_all_tools(session: ClientSession) -> list[Any]:
    """Every tool the server exposes, across ``tools/list`` pages.

    All of them are bound: a server's tools are hidden from the model until
    ``tool_search`` loads one, so a large server costs a catalog line per tool
    (up to ``max_tools``), not a schema. Truncating to ``max_tools`` here — as
    this once did — made the rest unreachable: google-drive exposed 128 tools
    and the agent could call 32.
    """
    tools: list[Any] = []
    cursor: str | None = None
    for _ in range(_MAX_TOOL_PAGES):
        listing = await session.list_tools(
            params=PaginatedRequestParams(cursor=cursor) if cursor else None
        )
        tools.extend(listing.tools)
        cursor = sdk_field(listing, "next_cursor", "nextCursor")
        if not cursor:
            return tools
    logger.warning(
        "MCP tools/list still paginating after %d pages; using %d tools",
        _MAX_TOOL_PAGES,
        len(tools),
    )
    return tools


async def start_manager_for_workspace(
    workspace: Path | None,
    *,
    factory: Callable[[], MCPClientManager] | None = None,
) -> MCPClientManager | None:
    """Standalone-CLI entry point: load global + workspace config, start a manager.

    Returns ``None`` when no server is configured — there is nothing to own,
    and callers behave exactly as they did before MCP existed. The file I/O
    runs off the loop; the sessions themselves are created on the caller's
    loop, which is why the caller must also be the one to ``stop()`` it
    (``AgentBundle.aclose``). *factory* exists for tests.
    """
    cfg = await asyncio.to_thread(MCPConfig.load, workspace)
    if not cfg.servers:
        return None
    manager = (factory or MCPClientManager)()
    await manager.start(cfg)
    return manager
