"""Standalone check for the event-loop-affinity fixes (no pytest, no langgraph).

Spins two real event loops in two threads — the same topology as the daemon
main loop + the async-subagent-host uvicorn loop — and verifies each chokepoint
from Architecture.md "Event-loop ownership":

  1. LoopLocal hands out one instance per loop (get + aget, race-free).
  2. loop_pinned: cross-loop use of one model raises the descriptive error;
     a dead pinned loop re-pins instead of raising.
  3. Embedder builds a distinct httpx.AsyncClient per loop.
  4. MCP tool_adapter marshals a foreign-loop call onto the session's home loop.
  5. PgPool (only if local Postgres answers): open() on loop A, SELECT 1 from
     loop B via a lazily-opened secondary pool; connection() before open() raises.

Run:  .venv/bin/python scripts/verify_loop_affinity.py
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "ok " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not ok else ""))
    PASS += ok
    FAIL += not ok


class LoopThread:
    """A running event loop on its own thread, driven from the main thread."""

    def __init__(self, name: str) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name=name, daemon=True
        )
        self.thread.start()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=30)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


def main() -> int:
    a = LoopThread("loop-A")
    b = LoopThread("loop-B")

    # ── 1. LoopLocal ────────────────────────────────────────────────────
    print("LoopLocal")
    from yuyutsava.aio import LoopLocal

    counter = {"n": 0}

    def make() -> object:
        counter["n"] += 1
        return object()

    ll: LoopLocal[object] = LoopLocal(make)

    async def _get():
        return ll.get()

    ia1, ia2 = a.run(_get()), a.run(_get())
    ib = b.run(_get())
    check("same instance on repeat (loop A)", ia1 is ia2)
    check("distinct instances across loops", ia1 is not ib)
    check("factory ran once per loop", counter["n"] == 2)

    acount = {"n": 0}
    all2: LoopLocal[object] = LoopLocal()

    async def _afactory():
        acount["n"] += 1
        await asyncio.sleep(0.01)
        return object()

    async def _aget_pair():
        return await asyncio.gather(all2.aget(_afactory), all2.aget(_afactory))

    r1, r2 = a.run(_aget_pair())
    check("aget: concurrent first calls create once", r1 is r2 and acount["n"] == 1)

    # ── 2. loop_pinned ──────────────────────────────────────────────────
    print("loop_pinned quirk")
    from yuyutsava.llm.quirks.loop_affinity import loop_pinned

    class FakeSDKModel:
        """Stands in for ChatVertexAI: caches an 'async client' per instance."""

        def __init__(self) -> None:
            self.async_client = None

        async def _agenerate(self, *args, **kwargs):
            if self.async_client is None:
                self.async_client = object()
            return "gen"

    Pinned = loop_pinned(FakeSDKModel)
    check("class identity is cached", loop_pinned(FakeSDKModel) is Pinned)
    m = Pinned()

    check("first use pins (loop A)", a.run(m._agenerate()) == "gen")
    check("second use on home loop fine", a.run(m._agenerate()) == "gen")
    try:
        b.run(m._agenerate())
        check("cross-loop use raises", False, "no error raised")
    except RuntimeError as exc:
        check(
            "cross-loop use raises the descriptive error",
            "pinned to another event loop" in str(exc)
            and "Event-loop ownership" in str(exc),
            str(exc),
        )

    old_client = m.async_client
    a.stop()  # kill the home loop → next use must re-pin, not raise
    check("dead home loop re-pins on loop B", b.run(m._agenerate()) == "gen")
    check("re-pin dropped the stale cached client", m.async_client is not old_client)
    a = LoopThread("loop-A2")  # fresh loop A for the remaining checks

    # ── 3. Embedder per-loop client ─────────────────────────────────────
    print("Embedder")
    from yuyutsava.memory.config import MemorySettings
    from yuyutsava.memory.embedder import Embedder

    emb = Embedder(MemorySettings())

    async def _client():
        return emb._clients.get()

    ca, cb = a.run(_client()), b.run(_client())
    check("distinct httpx clients per loop", ca is not cb)
    check("stable client per loop", a.run(_client()) is ca)
    a.run(emb.aclose())
    check("aclose closes only current loop's client", ca.is_closed and not cb.is_closed)

    # ── 4. MCP marshaling ───────────────────────────────────────────────
    print("MCP tool_adapter")
    from yuyutsava.mcp.tool_adapter import adapt

    class FakeTool:
        name = "echo"
        description = "echo"
        inputSchema = {"type": "object", "properties": {"x": {"type": "string"}}}

    class FakeResult:
        def __init__(self, text: str) -> None:
            self.content = [type("T", (), {"text": text})()]
            self.isError = False

    class FakeSession:
        def __init__(self) -> None:
            self.called_on: asyncio.AbstractEventLoop | None = None

        async def call_tool(self, name: str, args: dict):
            self.called_on = asyncio.get_running_loop()
            return FakeResult(f"{name}:{args.get('x')}")

    session = FakeSession()

    async def _adapt():
        return adapt(session, "srv", FakeTool())

    tool = a.run(_adapt())  # home loop = A

    async def _invoke():
        return await tool.coroutine(x="hi")

    out_home = a.run(_invoke())
    check("home-loop call is direct", out_home == "echo:hi" and session.called_on is a.loop)
    session.called_on = None
    out_foreign = b.run(_invoke())
    check(
        "foreign-loop call marshals to home loop",
        out_foreign == "echo:hi" and session.called_on is a.loop,
    )

    # ── 5. PgPool (optional: needs local Postgres) ──────────────────────
    print("PgPool")
    from yuyutsava.storage.backend import StorageSettings
    from yuyutsava.storage.pg.pool import PgPool

    pool = PgPool(StorageSettings.from_env())

    async def _select_one():
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT 1")
            row = await cur.fetchone()
            return row[0]

    try:
        b.run(_select_one())
        check("connection() before open() raises", False, "no error raised")
    except RuntimeError as exc:
        check("connection() before open() raises", "open() must be called first" in str(exc))

    try:
        a.run(pool.open(timeout_sec=3))
        pg_up = True
    except Exception as exc:  # noqa: BLE001
        pg_up = False
        print(f"  [skip] Postgres not reachable ({type(exc).__name__}) — pool checks skipped")
    if pg_up:
        check("query on primary loop", a.run(_select_one()) == 1)
        check("query from second loop via secondary pool", b.run(_select_one()) == 1)
        a.run(pool.close())
        try:
            a.run(_select_one())
            check("closed pool raises", False, "no error raised")
        except RuntimeError:
            check("closed pool raises", True)

    a.stop()
    b.stop()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
