"""
FastAPI server backing the persistent web window.

Endpoints:
- GET  /                          — static HTML
- GET  /static/{file}             — JS/CSS
- GET  /stream                    — SSE stream of ChannelEvents and pending asks
- POST /proposal/{id}/respond     — Tier-1 consent decision
- POST /ask/{id}/respond          — Tier-2 tool-permission response
- GET  /rules                     — list consent_rules
- DELETE /rules/{id}              — revoke a rule
- GET  /decisions                 — recent decisions for the timeline

The server is also a ``UserChannel``: ``WebChannel`` shares state with the
FastAPI app instance via the ``WebHub`` singleton.

Bind is loopback-only by contract. We refuse non-loopback hosts to avoid
accidentally exposing the agent to the local network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from yuyutsava.daemon.channels import (
    AskPrompt, ChannelEvent, ProposalDecision, UserChannel,
)
from yuyutsava.events.store import ConsentRule, Proposal, Store

logger = logging.getLogger("yuyutsava.daemon.web")

_STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Hub: shared state between the FastAPI handlers and the WebChannel
# ---------------------------------------------------------------------------


class WebHub:
    """Holds pending proposals/asks and the SSE broadcast queue."""

    def __init__(self, store: Store) -> None:
        self.store = store
        # Subscribers to /stream — each gets its own queue.
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._lock = asyncio.Lock()
        # Pending awaits, keyed by id.
        self.pending_proposals: dict[str, asyncio.Future[ProposalDecision]] = {}
        self.pending_asks: dict[str, asyncio.Future[str]] = {}

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers.append(q)
        try:
            while True:
                item = await q.get()
                yield item
        finally:
            async with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)

    async def broadcast(self, item: dict[str, Any]) -> None:
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass  # drop silently for slow tabs


# ---------------------------------------------------------------------------
# WebChannel: the UserChannel implementation that pushes through the hub
# ---------------------------------------------------------------------------


class WebChannel(UserChannel):
    name = "web"

    def __init__(self, hub: WebHub) -> None:
        self._hub = hub

    async def post_event(self, ev: ChannelEvent) -> None:
        await self._hub.broadcast({
            "type": "event",
            "kind": ev.kind,
            "data": ev.data,
        })

    async def post_proposal(self, p: Proposal) -> ProposalDecision:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ProposalDecision] = loop.create_future()
        self._hub.pending_proposals[p.proposal_id] = fut
        await self._hub.broadcast({
            "type": "proposal",
            "proposal": dataclasses.asdict(p),
        })
        timeout = max(1.0, p.expires_ts - time.time())
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return ProposalDecision(decision="expired")
        finally:
            self._hub.pending_proposals.pop(p.proposal_id, None)

    async def post_ask(self, a: AskPrompt) -> str:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._hub.pending_asks[a.ask_id] = fut
        await self._hub.broadcast({
            "type": "ask",
            "ask": {
                "ask_id": a.ask_id,
                "title": a.title,
                "body": a.body,
                "options": a.options,
            },
        })
        try:
            return await fut
        finally:
            self._hub.pending_asks.pop(a.ask_id, None)


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def make_app(hub: WebHub, *, host: str) -> FastAPI:
    if not (host.startswith("127.") or host == "localhost" or host == "::1"):
        raise RuntimeError(
            f"Refusing to bind to non-loopback host {host!r}. "
            "The web window is single-user and not authenticated for network access."
        )

    app = FastAPI(title="YUYUTSAVA daemon", docs_url=None, redoc_url=None)

    @app.get("/")
    async def index() -> Any:
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/static/{name}")
    async def static_file(name: str) -> Any:
        path = (_STATIC_DIR / name).resolve()
        if not str(path).startswith(str(_STATIC_DIR.resolve())) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    @app.get("/stream")
    async def stream(request: Request) -> EventSourceResponse:
        async def gen():
            # On reconnect, send a hello so the page knows we're up.
            yield {"event": "hello", "data": json.dumps({"ts": time.time()})}
            async for item in hub.subscribe():
                if await request.is_disconnected():
                    return
                yield {"event": item.get("type", "event"), "data": json.dumps(item, default=str)}
        return EventSourceResponse(gen())

    @app.post("/proposal/{proposal_id}/respond")
    async def respond_proposal(proposal_id: str, request: Request) -> Any:
        body = await request.json()
        decision = body.get("decision")
        if decision not in {"approve", "approve_remember", "modify", "skip", "skip_remember"}:
            raise HTTPException(400, f"invalid decision {decision!r}")

        # Atomic flip in the DB so a duplicate click doesn't double-resolve.
        flipped = hub.store.try_set_proposal_status(
            proposal_id, from_status="pending",
            to_status="approved" if decision in ("approve", "approve_remember") else
                      "modified" if decision == "modify" else "skipped",
        )
        if not flipped:
            raise HTTPException(410, "proposal expired or already resolved")

        fut = hub.pending_proposals.get(proposal_id)
        if fut is None or fut.done():
            return {"ok": True, "note": "no listener (already resolved)"}

        edited = body.get("edited_instruction") if decision == "modify" else None
        fut.set_result(ProposalDecision(decision=decision, edited_instruction=edited))
        return {"ok": True}

    @app.post("/ask/{ask_id}/respond")
    async def respond_ask(ask_id: str, request: Request) -> Any:
        body = await request.json()
        response = str(body.get("response", "")).strip() or "reject"
        fut = hub.pending_asks.get(ask_id)
        if fut is None or fut.done():
            raise HTTPException(410, "ask expired or already answered")
        fut.set_result(response)
        return {"ok": True}

    @app.get("/rules")
    async def list_rules() -> Any:
        return JSONResponse(hub.store.list_consent_rules())

    @app.delete("/rules/{rule_id}")
    async def delete_rule(rule_id: str) -> Any:
        # Synchronous direct delete on the connection — small, single-row op.
        conn = hub.store._conn  # type: ignore[attr-defined]
        if conn is None:
            raise HTTPException(503, "store not started")
        cur = conn.execute("DELETE FROM consent_rules WHERE rule_id=?", (rule_id,))
        conn.commit()
        return {"deleted": cur.rowcount}

    @app.get("/decisions")
    async def list_decisions(limit: int = 50) -> Any:
        return JSONResponse(hub.store.list_decisions(limit=min(max(1, limit), 500)))

    return app
