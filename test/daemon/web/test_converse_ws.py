"""WS /ws/converse protocol test with a stub ConversationManager.

Validates the transport contract (hello → token/final → turn_end, inline ask
round-trip) without building a real agent bundle.

Run:  .venv/bin/python -m pytest test/daemon/web/test_converse_ws.py
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from test.support.converse import FakeConversationManager
from test.support.ws import collect_until
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.app_deps import AppDeps
from yuyutsava.daemon.web.services.stream_service import WebHub


class _RecordingStore:
    async def put_event_payload(self, **kw) -> None: ...
    async def put_proposal(self, p) -> None: ...
    async def put_decision(self, **kw) -> None: ...


def _make_app():
    mgr = FakeConversationManager()
    app = create_app(
        WebHub(store=_RecordingStore()), host="127.0.0.1", deps=AppDeps(conversation_manager=mgr)
    )
    return app, mgr


def _drain_turn(ws):
    """Frames up to and including turn_end — failing, not parking, on a hang."""
    return collect_until(ws, "turn_end")


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
        seen = collect_until(ws, "ask")
        ask = seen[-1]
        # The ask frame carries the same wire record every surface renders
        # (AskPrompt.to_wire_dict), under "ask".
        assert ask["ask"]["body"] == "proceed?"
        assert ask["ask"]["ask_id"]
        # Answered WITHOUT an ask_id, as the app's client does when it has none:
        # the server resolves the run's pending ask. (test_converse_frames covers
        # the answer that names its ask_id.)
        ws.send_json({"type": "ask_response", "text": "yes go"})
        frames = _drain_turn(ws)
        text = "".join(f.get("text", "") for f in (seen + frames) if f["type"] == "token")
        assert "[yes go]" in text
        assert any(
            f["type"] == "ask_resolved" and f["ask_id"] == ask["ask"]["ask_id"] for f in frames
        )


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
