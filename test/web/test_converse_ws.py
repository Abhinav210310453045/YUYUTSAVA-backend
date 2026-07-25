"""WS /ws/converse protocol test with a stub ConversationManager.

Validates the transport contract (hello → token/final → turn_end, inline ask
round-trip) without building a real agent bundle.

Run:  uv run python -m pytest test/web/test_converse_ws.py -v
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from yuyutsava.core.streaming import StreamEvent
from yuyutsava.daemon.turn_registry import TurnRegistry
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.services.stream_service import WebHub


class _RecordingStore:
    async def put_event_payload(self, **kw) -> None: ...
    async def put_proposal(self, p) -> None: ...
    async def put_decision(self, **kw) -> None: ...


class _FakeConvo:
    def __init__(self, origin: str) -> None:
        self.session_id = "sess-123"
        self.thread_id = f"{origin}-123"
        self.finished = None

    @property
    def bundle_ready(self) -> bool:
        return True

    async def discard_if_unused(self) -> bool:
        return False

    async def run_turn(self, text, *, on_event, ask_handler=None, **_kw) -> str:
        await on_event(StreamEvent("token", {"text": "hi "}))
        if "ask" in text and ask_handler is not None:
            ans = await ask_handler({"type": "user_question", "body": "proceed?"})
            await on_event(StreamEvent("token", {"text": f"[{ans}]"}))
        if "slow" in text:
            await asyncio.sleep(5)  # cancelled by an interrupt in the test
        await on_event(StreamEvent("final", {"text": "done"}))
        return "done"

    async def finish(self, status: str = "done") -> None:
        self.finished = status


class _FakeManager:
    def __init__(self) -> None:
        self.opened = []
        # Turns are daemon-owned now: the router asks the manager for the
        # registry and hands it turn bodies (see daemon/turn_registry.py).
        self.turns = TurnRegistry()

    async def open(self, *, agent="master", card_id=None, origin="cli",
                   resume_id=None, continue_latest=False):
        convo = _FakeConvo(origin)
        self.opened.append((origin, resume_id, continue_latest))
        return convo, False

    def start_turn(self, thread_id: str, **kw):
        return self.turns.start(thread_id=thread_id, **kw)

    def record_async_launch(self, ev, *, thread_id, origin=None) -> None: ...


def _make_app():
    mgr = _FakeManager()
    app = create_app(
        WebHub(store=_RecordingStore()),
        host="127.0.0.1",
        conversation_manager=mgr,
    )
    return app, mgr


def _drain_turn(ws):
    """Collect frames until turn_end; return list of frames."""
    frames = []
    while True:
        f = ws.receive_json()
        frames.append(f)
        if f["type"] == "turn_end":
            return frames


def test_hello_and_basic_turn():
    app, mgr = _make_app()
    with TestClient(app).websocket_connect("/ws/converse?origin=voice") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["origin"] == "voice"
        assert hello["session_id"] == "sess-123"

        ws.send_json({"type": "user_text", "text": "hello there"})
        frames = _drain_turn(ws)
        kinds = [f["type"] for f in frames]
        assert "token" in kinds
        assert "final" in kinds
        assert kinds[-1] == "turn_end"
        text = "".join(f.get("text", "") for f in frames if f["type"] == "token")
        assert "hi" in text
    assert mgr.opened == [("voice", None, False)]


def test_inline_ask_roundtrip():
    app, _ = _make_app()
    with TestClient(app).websocket_connect("/ws/converse") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "user_text", "text": "please ask me"})
        # First frame(s): a token, then an ask.
        ask = None
        seen = []
        while ask is None:
            f = ws.receive_json()
            seen.append(f)
            if f["type"] == "ask":
                ask = f
        assert ask["payload"]["body"] == "proceed?"
        ws.send_json({"type": "ask_response", "text": "yes go"})
        frames = _drain_turn(ws)
        text = "".join(f.get("text", "") for f in (seen + frames) if f["type"] == "token")
        assert "[yes go]" in text


def test_ping_pong():
    app, _ = _make_app()
    with TestClient(app).websocket_connect("/ws/converse") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_interrupt_cancels_turn():
    app, _ = _make_app()
    with TestClient(app).websocket_connect("/ws/converse") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "user_text", "text": "slow task"})
        ws.receive_json()  # first token
        ws.send_json({"type": "interrupt"})
        frames = _drain_turn(ws)
        # cancellation surfaces a log then turn_end (no 'final')
        assert frames[-1]["type"] == "turn_end"
        assert all(f["type"] != "final" for f in frames)
