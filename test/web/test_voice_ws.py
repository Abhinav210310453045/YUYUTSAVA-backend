"""WS /ws/converse voice-path test with a stubbed VoicePipeline.

Validates the audio transport contract — mic frames → speech_started /
transcript → spoken reply (speaking_start / audio_chunk / speaking_end) → turn_end
— and barge-in, without real STT/TTS models or a real agent bundle.

Run:  uv run python -m pytest test/web/test_voice_ws.py -v
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from fastapi.testclient import TestClient

import yuyutsava.daemon.web.routers.converse as converse_router
from yuyutsava.audio_io.vad import VadResult
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

    @property
    def bundle_ready(self) -> bool:
        return True

    async def run_turn(self, text, *, on_event, ask_handler=None, **_kw) -> str:
        await on_event(StreamEvent("token", {"text": "Hello back. "}))
        if "slow" in text:
            await asyncio.sleep(5)  # cancelled by a barge-in in the test
        await on_event(StreamEvent("final", {"text": "Hello back."}))
        return "Hello back."

    async def finish(self, status: str = "done") -> None: ...

    async def discard_if_unused(self) -> bool:
        return False


class _FakeManager:
    def __init__(self) -> None:
        # Turns are daemon-owned now (see daemon/turn_registry.py).
        self.turns = TurnRegistry()

    async def open(self, *, agent="master", card_id=None, origin="cli",
                   resume_id=None, continue_latest=False):
        return _FakeConvo(origin), False

    def start_turn(self, thread_id: str, **kw):
        return self.turns.start(thread_id=thread_id, **kw)

    def record_async_launch(self, ev, *, thread_id, origin=None) -> None: ...


class _FakeVoice:
    """Deterministic VAD/STT/TTS stand-in driven by marker frames."""

    def feed_audio(self, pcm: bytes) -> VadResult:
        if pcm == b"START":
            return VadResult(speech_started=True)
        if pcm == b"END":
            return VadResult(utterance=b"normal")
        if pcm == b"ENDSLOW":
            return VadResult(utterance=b"slow")
        return VadResult()

    async def transcribe(self, pcm: bytes) -> str:
        return "slow task" if pcm == b"slow" else "hello agent"

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        return (b"\x01\x00" * 50, 22050)

    def flush(self) -> bytes | None:
        return None

    def reset(self) -> None: ...
    def close(self) -> None: ...


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(converse_router, "VoicePipeline", _FakeVoice)
    return create_app(
        WebHub(store=_RecordingStore()),
        host="127.0.0.1",
        conversation_manager=_FakeManager(),
    )


def _audio(marker: str) -> dict:
    return {"type": "audio", "pcm": base64.b64encode(marker.encode()).decode()}


def _drain_to_turn_end(ws) -> list[dict]:
    frames = []
    while True:
        f = ws.receive_json()
        frames.append(f)
        if f["type"] == "turn_end":
            return frames


def test_voice_turn_speaks_reply(app):
    with TestClient(app).websocket_connect("/ws/converse?origin=voice") as ws:
        assert ws.receive_json()["type"] == "hello"

        ws.send_json(_audio("START"))
        assert ws.receive_json()["type"] == "speech_started"

        ws.send_json(_audio("END"))
        frames = _drain_to_turn_end(ws)
        kinds = [f["type"] for f in frames]

        transcript = next(f for f in frames if f["type"] == "transcript")
        assert transcript["text"] == "hello agent"
        assert "speaking_start" in kinds
        assert "audio_chunk" in kinds
        assert "speaking_end" in kinds
        assert "final" in kinds
        assert kinds[-1] == "turn_end"

        chunk = next(f for f in frames if f["type"] == "audio_chunk")
        assert chunk["sample_rate"] == 22050
        assert base64.b64decode(chunk["pcm"])  # non-empty PCM


def test_barge_in_cancels_spoken_turn(app):
    with TestClient(app).websocket_connect("/ws/converse?origin=voice") as ws:
        assert ws.receive_json()["type"] == "hello"

        ws.send_json(_audio("ENDSLOW"))
        # transcript + first token arrive before the turn stalls on sleep().
        assert ws.receive_json()["type"] == "transcript"
        assert ws.receive_json()["type"] == "token"

        # Speak over the agent → barge-in cancels the turn.
        ws.send_json(_audio("START"))
        frames = _drain_to_turn_end(ws)
        assert frames[-1]["type"] == "turn_end"
        assert all(f["type"] != "final" for f in frames)
