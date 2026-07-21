"""Process-entry event-loop bootstrap.

Why this exists
---------------
On Windows, ``asyncio.run`` builds the default ``ProactorEventLoop``, but
psycopg's async pool (``AsyncConnectionPool``, ``AsyncPostgresSaver``) refuses
to run on it and raises "cannot use the 'ProactorEventLoop'". The whole storage
layer (Postgres/pgvector checkpoints, artifacts, memories) is therefore
unreachable on a native-Windows daemon unless the loop is a ``SelectorEventLoop``.

We install ``WindowsSelectorEventLoopPolicy`` once, at process entry, *before*
any loop is created. It is process-global on purpose: the AsyncSubagentHost runs
langgraph's ``run_server`` on its own thread loop (whose construction we do not
control) and background subagents touch psycopg there too — a global policy is
the only way to make that loop a Selector loop as well. This is safe because
langgraph_api / langgraph_runtime_inmem / uvicorn do not spawn asyncio
subprocesses and do not depend on the Proactor loop.

The compensating cost — a Selector loop cannot ``create_subprocess_exec`` — is
paid in :func:`yuyutsava.platform.process.run_capture`, which runs our one-shot
spawners (PowerShell via TaskRunner, etc.) in a worker thread on Windows.

On POSIX this is a byte-for-byte passthrough to ``asyncio.run`` — no policy is
touched, so macOS/Linux behavior is unchanged.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Awaitable, TypeVar

T = TypeVar("T")


def run(coro: "Awaitable[T]") -> T:
    """``asyncio.run``, but on Windows install the Selector loop policy first."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(coro)  # type: ignore[arg-type]


__all__ = ["run"]
