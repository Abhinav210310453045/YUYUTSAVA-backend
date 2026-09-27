"""Characterization: the `/ws/converse` inbound frame contract, frame by frame.

Written for `R-RDB-01`. `converse` is 801 lines with every frame type as a branch
inside one function, so before it becomes a `ConverseSession` with one handler
per frame, this pins what each branch does today — against the real router, with
a stub conversation manager, so the assertions describe the wire protocol rather
than the implementation.

**Deliberately not covered here, with reasons:**

- *Voice and barge-in* (`audio` on the main loop, the TTS worker). `R-TST-11`
  records why: the existing voice test's flow predates the mic gate that ignores
  input for the duration of an active turn, so driving that path from a test
  blocks on a `speech_started` that never comes.
- *Dictation mode* (`?mode=dictate`). Its loop is genuinely separate — no turn
  registry, no asks, no barge-in — and needs a stubbed `VoicePipeline`; it is its
  own unit and gets its own tests when it moves.

What remains is the whole text path plus the protocol's error handling, which is
what the split has to preserve.
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


def _app():
    mgr = FakeConversationManager()
    app = create_app(
        WebHub(store=_RecordingStore()), host="127.0.0.1", deps=AppDeps(conversation_manager=mgr)
    )
    return app, mgr


def _drain_to_turn_end(ws) -> list[dict]:
    frames = []
    while True:
        f = ws.receive_json()
        frames.append(f)
        if f["type"] == "turn_end":
            return frames


# --------------------------------------------------------------------------
# Handshake
# --------------------------------------------------------------------------


def test_hello_describes_the_connection():
    app, mgr = _app()
    with TestClient(app).websocket_connect("/ws/converse?origin=voice") as ws:
        hello = ws.receive_json()
    assert hello["type"] == "hello"
    assert hello["origin"] == "voice"
    assert hello["session_id"] == "sess-123"
    assert hello["resuming"] is False
    # The client needs these at connect to render the right affordances before
    # its first SSE frame: whether barge-in is on, and the voice toggles.
    assert "barge_in" in hello
    assert "voice" in hello
    assert mgr.opened == [("voice", None, False)]


# --------------------------------------------------------------------------
# Connection-scoped frames
# --------------------------------------------------------------------------


def test_ping_is_answered_with_pong():
    app, _ = _app()
    with TestClient(app).websocket_connect("/ws/converse") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_an_unknown_type_is_reported_not_ignored():
    app, _ = _app()
    with TestClient(app).websocket_connect("/ws/converse") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "no_such_frame"})
        frame = ws.receive_json()
    assert frame["type"] == "error"
    assert "no_such_frame" in frame["message"]


def test_a_malformed_frame_is_reported_and_the_socket_survives():
    app, _ = _app()
    with TestClient(app).websocket_connect("/ws/converse") as ws:
        ws.receive_json()  # hello
        ws.send_text("{not json")
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert "JSON" in frame["message"]
        # The connection must stay usable after a bad frame.
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


# --------------------------------------------------------------------------
# user_text — the whole text turn
# --------------------------------------------------------------------------


def test_user_text_runs_a_turn_and_closes_it():
    app, _ = _app()
    with TestClient(app).websocket_connect("/ws/converse") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "user_text", "text": "hello there"})
        frames = _drain_to_turn_end(ws)
    kinds = [f["type"] for f in frames]
    assert kinds[0] == "turn_start"
    assert "token" in kinds
    assert "final" in kinds
    assert kinds[-1] == "turn_end"
    # Every turn frame carries a monotonic seq so a reconnecting client can
    # replay the gap; connection-scoped frames (pong) carry none.
    seqs = [f["seq"] for f in frames if "seq" in f]
    assert seqs == sorted(seqs)


def test_blank_user_text_starts_no_turn():
    app, mgr = _app()
    with TestClient(app).websocket_connect("/ws/converse") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "user_text", "text": "   "})
        # Nothing should run; the socket answers the next frame normally.
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
    assert mgr.turn_texts == []


def test_selection_context_is_wrapped_in_the_sentinel():
    """Board-UI selection rides a separate field and is composed server-side.

    The wrapper is canonical here so clients can strip it from hydrated
    transcripts and show the user only what they typed.
    """
    app, mgr = _app()
    with TestClient(app).websocket_connect("/ws/converse") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "user_text", "text": "explain this", "context": "note:42"})
        _drain_to_turn_end(ws)
    assert mgr.turn_texts == ["<selection-context>\nnote:42\n</selection-context>\n\nexplain this"]


def test_interrupt_ends_the_turn_without_a_final():
    app, _ = _app()
    with TestClient(app).websocket_connect("/ws/converse") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "user_text", "text": "slow task"})
        assert ws.receive_json()["type"] == "turn_start"
        assert ws.receive_json()["type"] == "token"
        ws.send_json({"type": "interrupt"})
        frames = _drain_to_turn_end(ws)
    assert frames[-1]["type"] == "turn_end"
    assert all(f["type"] != "final" for f in frames), "a cancelled turn has no final"


def test_an_answered_ask_resumes_the_turn():
    """`R-BUG-08`: the answer must reach whatever the turn is actually waiting on.

    ``_ask`` parks on a run-local future when no channel router is wired, but
    ``create_app`` always builds a DecisionService — so a handler choosing on
    ``decisions`` alone answered a hub that had never seen the ask, and the turn
    hung forever with no ``final`` and no ``turn_end``.
    """
    app, _ = _app()
    with TestClient(app).websocket_connect("/ws/converse") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "user_text", "text": "ask me something"})
        asked = collect_until(ws, "ask")[-1]
        assert asked["ask"]["ask_id"]
        ws.send_json({"type": "ask_response", "ask_id": asked["ask"]["ask_id"], "text": "yes"})
        rest = collect_until(ws, "turn_end")

    kinds = [f["type"] for f in rest]
    assert "ask_resolved" in kinds, "the inline card is never cleared"
    assert "final" in kinds, "the turn produced no answer"
    assert kinds[-1] == "turn_end"
    # The agent resumed with the text, rather than the socket merely being told
    # the ask was resolved.
    assert any("[yes]" in f.get("text", "") for f in rest if f["type"] == "token")
