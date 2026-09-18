"""Characterization: the `?mode=dictate` loop, before it leaves the router.

`_dictate` is a second dispatch loop living inside `routers/converse.py`
(`R-RDB-01`). It shares the voice plumbing of a conversation — `VoicePipeline`
VAD → STT — but opens no session, runs no turn, and has no asks or barge-in, so
it is its own unit and moves out on its own. These tests pin what it sends today.

The pipeline is stubbed, so nothing here loads an STT model or touches audio
hardware: the fake returns `VadResult` / `TranscriptResult` shapes driven by
marker frames, which is how the existing voice test drives its own stub.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

import yuyutsava.daemon.web.routers.converse as converse_router
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.services.stream_service import WebHub
from yuyutsava.daemon.web.voice_pipeline import TranscriptResult
from yuyutsava.voice.vad import VadResult


class _RecordingStore:
    async def put_event_payload(self, **kw) -> None: ...
    async def put_proposal(self, p) -> None: ...
    async def put_decision(self, **kw) -> None: ...


class _FakeVoice:
    """VAD/STT stand-in driven by marker frames.

    ``SURE`` transcribes confidently; ``UNSURE`` comes back below
    `YUYUTSAVA_STT_MIN_CONFIDENCE` (default 0.35) and must be dropped; ``TAIL``
    is what a flush returns so `audio_end` has something to drain.
    """

    def __init__(self) -> None:
        self.flushed = False

    async def prewarm(self) -> None: ...

    def feed_audio(self, pcm: bytes) -> VadResult:
        if pcm == b"START":
            return VadResult(speech_started=True)
        if pcm in (b"SURE", b"UNSURE"):
            return VadResult(utterance=pcm)
        return VadResult()

    async def transcribe_detailed(self, pcm: bytes) -> TranscriptResult:
        if pcm == b"UNSURE":
            return TranscriptResult(text="maybe this", confidence=0.05)
        if pcm == b"TAIL":
            return TranscriptResult(text="the tail", confidence=0.9)
        return TranscriptResult(text="hello there", confidence=0.9)

    def flush(self) -> bytes | None:
        self.flushed = True
        return b"TAIL"

    def reset(self) -> None: ...
    def close(self) -> None: ...


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(converse_router, "VoicePipeline", _FakeVoice)
    return create_app(WebHub(store=_RecordingStore()), host="127.0.0.1", conversation_manager=None)


def _audio(marker: str) -> dict:
    return {"type": "audio", "pcm": base64.b64encode(marker.encode()).decode()}


def test_hello_announces_dictation_mode(app):
    with TestClient(app).websocket_connect("/ws/converse?mode=dictate") as ws:
        hello = ws.receive_json()
    # No session is opened in this mode, so the handshake carries the mode and
    # nothing else a conversation would need.
    assert hello == {"type": "hello", "mode": "dictate"}


def test_ping_is_answered_in_dictation_mode(app):
    with TestClient(app).websocket_connect("/ws/converse?mode=dictate") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_speech_onset_and_transcript(app):
    with TestClient(app).websocket_connect("/ws/converse?mode=dictate") as ws:
        ws.receive_json()  # hello
        ws.send_json(_audio("START"))
        assert ws.receive_json() == {"type": "speech_started"}
        ws.send_json(_audio("SURE"))
        frame = ws.receive_json()
    assert frame["type"] == "transcript"
    assert frame["text"] == "hello there"


def test_a_low_confidence_transcript_is_dropped(app):
    """Hallucinated noise is worse than a gap in an editable textarea."""
    with TestClient(app).websocket_connect("/ws/converse?mode=dictate") as ws:
        ws.receive_json()  # hello
        ws.send_json(_audio("UNSURE"))
        # Nothing is sent for it. Ask for the tail: the next frame proves the
        # dropped utterance produced no transcript of its own.
        ws.send_json({"type": "audio_end"})
        frames = [ws.receive_json()]
        while frames[-1]["type"] != "dictate_done":
            frames.append(ws.receive_json())
    texts = [f.get("text") for f in frames if f["type"] == "transcript"]
    assert "maybe this" not in texts
    assert texts == ["the tail"]


def test_audio_end_drains_the_tail_then_acks(app):
    with TestClient(app).websocket_connect("/ws/converse?mode=dictate") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "audio_end"})
        frames = [ws.receive_json()]
        while frames[-1]["type"] != "dictate_done":
            frames.append(ws.receive_json())
    kinds = [f["type"] for f in frames]
    # The ack lands behind the flushed tail, not before it.
    assert kinds == ["transcript", "dictate_done"]


def test_unknown_and_malformed_frames_are_reported(app):
    with TestClient(app).websocket_connect("/ws/converse?mode=dictate") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "no_such_frame"})
        first = ws.receive_json()
        assert first["type"] == "error"
        assert "no_such_frame" in first["message"]

        ws.send_text("{not json")
        second = ws.receive_json()
        assert second["type"] == "error"
        assert "JSON" in second["message"]

        # Still usable after both.
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
