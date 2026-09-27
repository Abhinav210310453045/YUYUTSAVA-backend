"""End-to-end validation of the async-subagents stack against a FastAPI server.

Exercises the full vertical slice except for the orchestrator's LLM dispatch:

    AsyncSubagentHost (in-process lg server)
      ↑
      |  langgraph_sdk
      ↓
    AsyncTaskHealthWatcher  ←—— ChannelRouter (with WebChannel + CliRemoteChannel)
      └── routes interrupts ──→  pending_asks (shared WebHub)
                                  └── consumed by an httpx client
                                       acting as the Electron renderer

Validates:
  1. The lg host serves and accepts threads/runs via SDK.
  2. A graph that calls ``interrupt()`` is detected by the watcher
     (thread.status == "interrupted" with run.status == "success").
  3. The watcher emits ``async_task_*`` events through ``ChannelRouter.post_event``
     and routes the ask via ``ChannelRouter.post_ask``.
  4. ``SessionOriginMap`` is honoured by the router.
  5. The ``/cli/attach`` + ``/cli/detach`` endpoints register/remove a
     ``CliRemoteChannel`` cleanly.
  6. The ``/stream`` SSE endpoint delivers the new ``async_task_*`` payload kinds.
  7. ``AsyncSubagent`` interrupt is resumed via SDK and the run completes.

Run::

    uv run python scripts/async_subagents_e2e.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field

import httpx
import uvicorn
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from langgraph_sdk import get_client
from typing_extensions import TypedDict

from yuyutsava.async_subagents.host import AsyncSubagentHost
from yuyutsava.async_subagents.mirror import AsyncTaskMirror, MirroredTask
from yuyutsava.async_subagents.session_origin import SessionOriginMap
from yuyutsava.async_subagents.watcher import AsyncTaskHealthWatcher
from yuyutsava.daemon.channels import ChannelRouter
from yuyutsava.daemon.cli_remote_channel import CliRemoteChannel
from yuyutsava.daemon.orchestrator_loop import make_ask_handler
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.app_deps import AppDeps
from yuyutsava.daemon.web.services.stream_service import WebChannel, WebHub
from yuyutsava.storage.events import Store

SESSION = "e2e-session"

# ---------------------------------------------------------------------------
# Mock subagent graph that interrupts once.
# ---------------------------------------------------------------------------


class _State(TypedDict, total=False):
    messages: list
    step: int


def _node_interrupt(state):
    reply = interrupt(
        {"type": "user_question", "question": "approve?", "options": ["approve", "reject"]}
    )
    return {
        "step": (state.get("step") or 0) + 1,
        "messages": [{"role": "assistant", "content": f"resumed with {reply!r}"}],
    }


def _node_finish(state):
    msgs = list(state.get("messages") or [])
    msgs.append({"role": "assistant", "content": "task done"})
    return {"messages": msgs}


def _build_mock_graph():
    g = StateGraph(_State)
    g.add_node("ask", _node_interrupt)
    g.add_node("finish", _node_finish)
    g.add_edge(START, "ask")
    g.add_edge("ask", "finish")
    g.add_edge("finish", END)
    return g.compile()


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Test orchestration
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self):
        self.failures: list[str] = []
        self.passes: list[str] = []

    def ok(self, what: str):
        self.passes.append(what)
        print(f"  \033[32m✓\033[0m {what}")

    def fail(self, what: str, detail: str = ""):
        self.failures.append(f"{what} — {detail}" if detail else what)
        print(f"  \033[31m✗\033[0m {what}  {detail}")


@dataclass
class _Stack:
    """Everything the run stands up, so each phase takes one argument."""

    lg_host: AsyncSubagentHost
    channels: ChannelRouter
    session_origin: SessionOriginMap
    mirror: AsyncTaskMirror
    watcher: AsyncTaskHealthWatcher
    api_base: str
    api_server: uvicorn.Server
    api_task: asyncio.Task


@dataclass
class _SseTap:
    """Reads ``/stream`` and keeps what the run checks: async_task_* kinds, the ask id."""

    api_base: str
    kinds: list[str] = field(default_factory=list)
    ask_id: str | None = None

    async def run(self) -> None:
        async with httpx.AsyncClient(base_url=self.api_base, timeout=None) as sc:
            async with sc.stream("GET", "/stream") as resp:
                current_event = "message"
                async for raw in resp.aiter_lines():
                    if raw.startswith("event:"):
                        current_event = raw.split(":", 1)[1].strip()
                    elif raw.startswith("data:"):
                        self._take(current_event, raw.split(":", 1)[1].strip())

    def _take(self, event: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        if event == "event":
            kind = data.get("kind") or ""
            if kind.startswith("async_task_"):
                self.kinds.append(kind)
        elif event == "ask" and self.ask_id is None:
            # Wire format nests under data["ask"]: {"type": "ask", "ask": {...}}
            self.ask_id = (data.get("ask") or data).get("ask_id")


async def _start_api(
    r: _Result,
) -> tuple[ChannelRouter, SessionOriginMap, str, uvicorn.Server, asyncio.Task]:
    """The daemon's FastAPI on a free port, with a web channel and an origin map."""
    api_port = _free_port()
    # No persistence needed: an in-memory events store.
    hub = WebHub(Store(":memory:"))
    channels = ChannelRouter(channels=[], primary_name="web")
    channels.channels.append(WebChannel(hub))
    session_origin = SessionOriginMap()
    channels.session_origin = session_origin

    app = create_app(
        hub, host="127.0.0.1", deps=AppDeps(channels=channels, session_origin=session_origin)
    )
    config = uvicorn.Config(
        app, host="127.0.0.1", port=api_port, log_level="warning", access_log=False, lifespan="on"
    )
    api_server = uvicorn.Server(config)
    api_task = asyncio.create_task(api_server.serve(), name="e2e-api")

    api_base = f"http://127.0.0.1:{api_port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=1.0) as c:
                if (await c.get(f"{api_base}/health")).status_code == 200:
                    break
        except httpx.HTTPError:
            await asyncio.sleep(0.1)
    else:
        raise RuntimeError(f"daemon FastAPI never answered /health on {api_base}")
    r.ok(f"daemon FastAPI ready ({api_base})")
    return channels, session_origin, api_base, api_server, api_task


async def _stand_up(r: _Result) -> _Stack:
    lg_host = AsyncSubagentHost(graphs={"e2e-mock": _build_mock_graph()})
    await asyncio.to_thread(lg_host.start)
    r.ok(f"AsyncSubagentHost ready ({lg_host.url})")
    try:
        channels, session_origin, api_base, api_server, api_task = await _start_api(r)
    except RuntimeError as exc:
        r.fail("daemon FastAPI bound", str(exc))
        lg_host.shutdown()
        raise
    mirror = AsyncTaskMirror()
    watcher = AsyncTaskHealthWatcher(
        mirror=mirror,
        host_url=lg_host.url,
        ask_handler=make_ask_handler(channels, default_session_id="e2e", default_agent_path="e2e"),
        event_sink=channels.post_event,
        agent_path_root="e2e",
        poll_interval_sec=0.5,
    )
    await watcher.start()
    r.ok("AsyncTaskHealthWatcher running")
    return _Stack(
        lg_host, channels, session_origin, mirror, watcher, api_base, api_server, api_task
    )


def _check(r: _Result, passed: bool, what: str, detail: object = "") -> None:
    if passed:
        r.ok(what)
    else:
        r.fail(what, str(detail))


async def _check_attach(r: _Result, st: _Stack, cli: httpx.AsyncClient) -> None:
    first = await cli.post("/cli/attach", json={"session_id": SESSION, "label": "e2e-test"})
    _check(
        r,
        first.status_code == 200 and first.json().get("attached") is True,
        "/cli/attach registered CliRemoteChannel",
        first.text,
    )
    _check(
        r,
        st.session_origin.get(SESSION) == "cli-remote",
        "SessionOriginMap recorded session→cli-remote",
        st.session_origin.snapshot(),
    )
    _check(
        r,
        any(isinstance(c, CliRemoteChannel) for c in st.channels.channels),
        "ChannelRouter contains a CliRemoteChannel",
    )
    # Idempotency: a second attach reports attached=False.
    again = await cli.post("/cli/attach", json={"session_id": SESSION})
    _check(
        r,
        again.status_code == 200 and again.json().get("attached") is False,
        "/cli/attach is idempotent",
        again.text,
    )


async def _start_bg_run(st: _Stack) -> str:
    """A run of the interrupting mock graph, mirrored as a background task."""
    lg = get_client(url=st.lg_host.url)
    thread = await lg.threads.create()
    await lg.runs.create(
        thread_id=thread["thread_id"],
        assistant_id="e2e-mock",
        input={"messages": [{"role": "user", "content": "go"}]},
    )
    now = time.time()
    await st.mirror.upsert(
        MirroredTask(
            task_id=thread["thread_id"],
            agent_name="e2e-mock-bg",
            graph_id="e2e-mock",
            instruction="go",
            status="running",
            started_at=now,
            last_update_at=now,
            sub_thread_id=thread["thread_id"],
            parent_thread_id=SESSION,
        )
    )
    return thread["thread_id"]


async def _answer_the_ask(r: _Result, tap: _SseTap, cli: httpx.AsyncClient) -> None:
    deadline = time.time() + 12
    while time.time() < deadline and tap.ask_id is None:
        await asyncio.sleep(0.15)
    if tap.ask_id is None:
        r.fail("ask did not surface on SSE within 12s")
        return
    r.ok(f"ask surfaced on SSE (ask_id={tap.ask_id[:8]})")
    resp = await cli.post(f"/ask/{tap.ask_id}/respond", json={"response": "approve"})
    _check(r, resp.status_code == 200, "/ask/{id}/respond accepted", resp.text)


async def _check_completion(r: _Result, st: _Stack, tap: _SseTap, task_id: str) -> None:
    deadline = time.time() + 12
    while time.time() < deadline:
        t = st.mirror.get(task_id)
        if t and t.is_terminal():
            break
        await asyncio.sleep(0.2)
    t = st.mirror.get(task_id)
    _check(
        r,
        bool(t and t.status == "success"),
        f"bg task reached terminal status: success (summary={t.summary if t else None!r})",
        f"status={t.status if t else None}",
    )
    await asyncio.sleep(0.4)  # let the last SSE events arrive
    for kind in ("async_task_awaiting_user", "async_task_completed"):
        _check(r, kind in tap.kinds, f"SSE delivered {kind}", tap.kinds)


async def _check_detach(r: _Result, st: _Stack, cli: httpx.AsyncClient) -> None:
    detach = await cli.post("/cli/detach", json={"session_id": SESSION})
    _check(r, detach.status_code == 200, "/cli/detach succeeded", detach.text)
    _check(
        r,
        not any(isinstance(c, CliRemoteChannel) for c in st.channels.channels),
        "CliRemoteChannel removed after detach",
    )
    _check(
        r,
        st.session_origin.get(SESSION) is None,
        "SessionOriginMap cleared after detach",
        st.session_origin.snapshot(),
    )


async def _tear_down(st: _Stack) -> None:
    await st.watcher.shutdown()
    st.api_server.should_exit = True
    try:
        await asyncio.wait_for(st.api_task, timeout=5.0)
    except TimeoutError:
        st.api_task.cancel()
    st.lg_host.shutdown()


async def _main() -> int:
    r = _Result()
    print("\n== E2E async-subagent stack validation ==\n")
    st = await _stand_up(r)

    async with httpx.AsyncClient(base_url=st.api_base, timeout=5.0) as cli:
        await _check_attach(r, st, cli)
        tap = _SseTap(st.api_base)
        sse_task = asyncio.create_task(tap.run(), name="e2e-sse")
        await asyncio.sleep(0.3)  # let the consumer attach
        task_id = await _start_bg_run(st)
        await _answer_the_ask(r, tap, cli)
        await _check_completion(r, st, tap, task_id)
        await _check_detach(r, st, cli)
        sse_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sse_task

    await _tear_down(st)

    print("\n== Summary ==")
    print(f"  passed: {len(r.passes)}")
    print(f"  failed: {len(r.failures)}")
    for fail in r.failures:
        print(f"    - {fail}")
    return 0 if not r.failures else 1


if __name__ == "__main__":
    rc = asyncio.run(_main())
    sys.stdout.flush()
    os._exit(rc)
