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

    uv run python test/async_subagents/e2e_stack.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
import uuid
from contextlib import closing

import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


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


async def _main() -> int:
    from yuyutsava.async_subagents.host import AsyncSubagentHost
    from yuyutsava.async_subagents.mirror import AsyncTaskMirror, MirroredTask
    from yuyutsava.async_subagents.session_origin import SessionOriginMap
    from yuyutsava.async_subagents.watcher import AsyncTaskHealthWatcher
    from yuyutsava.daemon.channels import ChannelRouter
    from yuyutsava.daemon.cli_remote_channel import CliRemoteChannel
    from yuyutsava.daemon.orchestrator_loop import make_ask_handler
    from yuyutsava.daemon.web.app import create_app
    from yuyutsava.daemon.web.services.stream_service import WebChannel, WebHub
    from yuyutsava.storage.events import Store

    import uvicorn

    r = _Result()
    print("\n== E2E async-subagent stack validation ==\n")

    # -- 1. Stand up the lg host --------------------------------------------
    lg_host = AsyncSubagentHost(graphs={"e2e-mock": _build_mock_graph()})
    await asyncio.to_thread(lg_host.start)
    r.ok(f"AsyncSubagentHost ready ({lg_host.url})")

    # -- 2. Stand up the daemon FastAPI on a free port ----------------------
    api_port = _free_port()
    store = Store.in_memory() if hasattr(Store, "in_memory") else None
    if store is None:
        # Fall back: build a minimal store via the default DB path so create_app
        # can be constructed. We don't need persistence for this test.
        try:
            from yuyutsava.storage.paths import state_dir  # noqa: F401
            store = Store(":memory:")
        except Exception as e:
            print(f"  (note: Store init failed — running without persistence): {e!r}")
            store = type("EmptyStore", (), {"stop": lambda self: None})()

    hub = WebHub(store)
    channels = ChannelRouter(channels=[], primary_name="web")
    channels.channels.append(WebChannel(hub))
    session_origin = SessionOriginMap()
    channels.session_origin = session_origin

    app = create_app(
        hub, host="127.0.0.1",
        channels=channels, session_origin=session_origin,
    )
    config = uvicorn.Config(
        app, host="127.0.0.1", port=api_port, log_level="warning",
        access_log=False, lifespan="on",
    )
    api_server = uvicorn.Server(config)
    api_task = asyncio.create_task(api_server.serve(), name="e2e-api")

    # Wait for the FastAPI server to bind.
    deadline = time.time() + 10
    api_base = f"http://127.0.0.1:{api_port}"
    while time.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=1.0) as c:
                resp = await c.get(f"{api_base}/health")
                if resp.status_code == 200:
                    break
        except httpx.HTTPError:
            await asyncio.sleep(0.1)
    else:
        r.fail("daemon FastAPI bound", f"no /health on {api_base}")
        lg_host.shutdown()
        os._exit(2)
    r.ok(f"daemon FastAPI ready ({api_base})")

    # -- 3. Start the watcher -----------------------------------------------
    mirror = AsyncTaskMirror()
    watcher = AsyncTaskHealthWatcher(
        mirror=mirror,
        host_url=lg_host.url,
        ask_handler=make_ask_handler(
            channels, default_session_id="e2e", default_agent_path="e2e",
        ),
        event_sink=channels.post_event,
        agent_path_root="e2e",
        poll_interval_sec=0.5,
    )
    await watcher.start()
    r.ok("AsyncTaskHealthWatcher running")

    # -- 4. /cli/attach roundtrip ------------------------------------------
    async with httpx.AsyncClient(base_url=api_base, timeout=5.0) as cli_http:
        attach_resp = await cli_http.post("/cli/attach", json={
            "session_id": "e2e-session", "label": "e2e-test",
        })
        if attach_resp.status_code == 200 and attach_resp.json().get("attached") is True:
            r.ok("/cli/attach registered CliRemoteChannel")
        else:
            r.fail("/cli/attach failed", str(attach_resp.text))

        # Confirm origin map was set.
        if session_origin.get("e2e-session") == "cli-remote":
            r.ok("SessionOriginMap recorded session→cli-remote")
        else:
            r.fail("SessionOriginMap not set", session_origin.snapshot())

        # Confirm channel was actually appended.
        cli_present = any(isinstance(c, CliRemoteChannel) for c in channels.channels)
        if cli_present:
            r.ok("ChannelRouter contains a CliRemoteChannel")
        else:
            r.fail("CliRemoteChannel missing")

        # Idempotency: a second attach should report attached=False.
        attach2 = await cli_http.post("/cli/attach", json={"session_id": "e2e-session"})
        if attach2.status_code == 200 and attach2.json().get("attached") is False:
            r.ok("/cli/attach is idempotent")
        else:
            r.fail("/cli/attach not idempotent", str(attach2.text))

        # -- 5. SSE delivers async_task_* kinds ---------------------------------
        # Open the SSE stream and collect events while we drive a bg run.
        collected_kinds: list[str] = []
        ask_id_holder: dict[str, str] = {}

        async def _sse_consumer():
            async with httpx.AsyncClient(base_url=api_base, timeout=None) as sc:
                async with sc.stream("GET", "/stream") as resp:
                    current_event = "message"
                    async for raw in resp.aiter_lines():
                        if not raw:
                            continue
                        if raw.startswith("event:"):
                            current_event = raw.split(":", 1)[1].strip()
                            continue
                        if raw.startswith("data:"):
                            try:
                                data = json.loads(raw.split(":", 1)[1].strip())
                            except json.JSONDecodeError:
                                continue
                            if current_event == "event":
                                kind = data.get("kind") or ""
                                if kind.startswith("async_task_"):
                                    collected_kinds.append(kind)
                            elif current_event == "ask":
                                # Wire format nests under data["ask"]:
                                #   {"type": "ask", "ask": {"ask_id": ..., ...}}
                                inner = data.get("ask") or data
                                ask_id = inner.get("ask_id")
                                if ask_id and "ask_id" not in ask_id_holder:
                                    ask_id_holder["ask_id"] = ask_id

        sse_task = asyncio.create_task(_sse_consumer(), name="e2e-sse")
        await asyncio.sleep(0.3)   # let the consumer attach

        # -- 6. Drive a bg run --------------------------------------------------
        from langgraph_sdk import get_client
        lg = get_client(url=lg_host.url)
        thread = await lg.threads.create()
        run = await lg.runs.create(
            thread_id=thread["thread_id"],
            assistant_id="e2e-mock",
            input={"messages": [{"role": "user", "content": "go"}]},
        )
        await mirror.upsert(MirroredTask(
            task_id=thread["thread_id"], agent_name="e2e-mock-bg",
            graph_id="e2e-mock", instruction="go", status="running",
            started_at=time.time(), last_update_at=time.time(),
            sub_thread_id=thread["thread_id"],
            parent_thread_id="e2e-session",
        ))

        # -- 7. Wait for the ask to surface, then respond ----------------------
        deadline = time.time() + 12
        while time.time() < deadline and "ask_id" not in ask_id_holder:
            await asyncio.sleep(0.15)
        if "ask_id" not in ask_id_holder:
            r.fail("ask did not surface on SSE within 12s")
        else:
            r.ok(f"ask surfaced on SSE (ask_id={ask_id_holder['ask_id'][:8]})")
            # POST the response.
            resp = await cli_http.post(
                f"/ask/{ask_id_holder['ask_id']}/respond",
                json={"response": "approve"},
            )
            if resp.status_code == 200:
                r.ok("/ask/{id}/respond accepted")
            else:
                r.fail("/ask/{id}/respond failed", resp.text)

        # -- 8. Wait for the task to reach terminal status --------------------
        deadline = time.time() + 12
        while time.time() < deadline:
            t = mirror.get(thread["thread_id"])
            if t and t.is_terminal():
                break
            await asyncio.sleep(0.2)
        t = mirror.get(thread["thread_id"])
        if t and t.status == "success":
            r.ok(f"bg task reached terminal status: success (summary={t.summary!r})")
        else:
            r.fail("bg task did not reach success", f"status={t.status if t else None}")

        # -- 9. Confirm payload kinds were emitted via SSE --------------------
        await asyncio.sleep(0.4)
        if any(k == "async_task_awaiting_user" for k in collected_kinds):
            r.ok("SSE delivered async_task_awaiting_user")
        else:
            r.fail("SSE missing async_task_awaiting_user", str(collected_kinds))
        if any(k == "async_task_completed" for k in collected_kinds):
            r.ok("SSE delivered async_task_completed")
        else:
            r.fail("SSE missing async_task_completed", str(collected_kinds))

        # -- 10. /cli/detach removes the channel + clears origin --------------
        detach = await cli_http.post("/cli/detach", json={"session_id": "e2e-session"})
        if detach.status_code == 200:
            r.ok("/cli/detach succeeded")
        else:
            r.fail("/cli/detach failed", detach.text)
        if not any(isinstance(c, CliRemoteChannel) for c in channels.channels):
            r.ok("CliRemoteChannel removed after detach")
        else:
            r.fail("CliRemoteChannel still present after detach")
        if session_origin.get("e2e-session") is None:
            r.ok("SessionOriginMap cleared after detach")
        else:
            r.fail("SessionOriginMap not cleared", session_origin.snapshot())

        sse_task.cancel()
        try:
            await sse_task
        except (asyncio.CancelledError, Exception):
            pass

    # -- Teardown -----------------------------------------------------------
    await watcher.shutdown()
    api_server.should_exit = True
    try:
        await asyncio.wait_for(api_task, timeout=5.0)
    except (asyncio.TimeoutError, Exception):
        api_task.cancel()
    lg_host.shutdown()

    # -- Summary ------------------------------------------------------------
    print(f"\n== Summary ==")
    print(f"  passed: {len(r.passes)}")
    print(f"  failed: {len(r.failures)}")
    for fail in r.failures:
        print(f"    - {fail}")
    return 0 if not r.failures else 1


if __name__ == "__main__":
    rc = asyncio.run(_main())
    sys.stdout.flush()
    os._exit(rc)
