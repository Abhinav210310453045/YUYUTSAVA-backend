"""Characterization: the voice path of `/ws/converse`, as it behaves today.

`R-TST-11`: the previous version of this file was written against an older
contract and could not run — its stub had no `prewarm`, it stubbed `transcribe`
where the router calls `transcribe_detailed`, and its barge-in case drove mic
frames straight into the mic gate, which drops audio for the whole duration of
an active turn unless barge-in is explicitly enabled. Patching those one at a
time turned a fast failure into a hang, so it is rewritten here against what the
router actually does.

Pinned before `converse` is split (`R-RDB-01`), because the voice machinery is
~280 lines of that function and nothing else covers it.

No STT/TTS models and no real agent: the pipeline is a stub driven by marker
frames, the way the dictation tests drive theirs.
"""

from __future__ import annotations

import asyncio
import base64
import threading

import pytest
from fastapi.testclient import TestClient

import yuyutsava.daemon.web.routers.converse as converse_router
from yuyutsava.core.streaming import StreamEvent
from yuyutsava.daemon.turn_registry import TurnRegistry
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.app_deps import AppDeps
from yuyutsava.daemon.web.services.stream_service import WebHub
from yuyutsava.daemon.web.voice_pipeline import TranscriptResult
from yuyutsava.voice.vad import VadResult


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

    async def discard_if_unused(self) -> bool:
        return False

    async def run_turn(self, text, *, on_event, ask_handler=None, **_kw) -> str:
        # A trailing period matters: the TTS side synthesizes per finished
        # sentence, so a fragment would never produce an audio chunk.
        await on_event(StreamEvent("token", {"text": "Hello back. "}))
        if "slow" in text:
            await asyncio.sleep(5)  # long enough to still be running mid-test
        await on_event(StreamEvent("final", {"text": "Hello back."}))
        return "Hello back."

    async def finish(self, status: str = "done") -> None: ...


class _FakeManager:
    def __init__(self) -> None:
        self.turns = TurnRegistry()

    async def open(
        self, *, agent="master", card_id=None, origin="cli", resume_id=None, continue_latest=False
    ):
        return _FakeConvo(origin), False

    def start_turn(self, thread_id: str, **kw):
        return self.turns.start(thread_id=thread_id, **kw)

    def record_async_launch(self, ev, *, thread_id, origin=None) -> None: ...


class _FakeVoice:
    """VAD/STT/TTS stand-in driven by marker frames.

    The markers name what the VAD decided, not what was said: ``START`` is a
    bare speech onset, and the others are completed utterances that transcribe
    to different things (normal words, silence, and a garbled low-confidence
    result).
    """

    async def prewarm(self) -> None: ...

    def feed_audio(self, pcm: bytes) -> VadResult:
        if pcm == b"START":
            return VadResult(speech_started=True)
        if pcm in (b"END", b"ENDSLOW", b"ENDEMPTY", b"ENDUNSURE"):
            return VadResult(utterance=pcm)
        return VadResult()

    async def transcribe_detailed(self, pcm: bytes) -> TranscriptResult:
        if pcm == b"ENDSLOW":
            return TranscriptResult(text="do something slow", confidence=0.9)
        if pcm == b"ENDEMPTY":
            return TranscriptResult(text="", confidence=0.9)
        if pcm == b"ENDUNSURE":
            return TranscriptResult(text="welcome to the general", confidence=0.05)
        return TranscriptResult(text="hello agent", confidence=0.9)

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        return (b"\x01\x00" * 50, 22050)

    def flush(self) -> bytes | None:
        return b"END"

    def set_speaking(self, speaking: bool) -> None:
        """Tightens the VAD while the agent talks, so its own TTS can't
        self-interrupt. The router calls this around every spoken reply, and a
        stub without it kills the TTS worker just after ``speaking_start``."""
        self.speaking = speaking

    def close(self) -> None: ...


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(converse_router, "VoicePipeline", _FakeVoice)
    return create_app(
        WebHub(store=_RecordingStore()),
        host="127.0.0.1",
        deps=AppDeps(conversation_manager=_FakeManager()),
    )


def _audio(marker: str) -> dict:
    return {"type": "audio", "pcm": base64.b64encode(marker.encode()).decode()}


def _collect_until(ws, until: str, timeout: float = 20.0) -> list[dict]:
    """Drain frames until ``until``, failing rather than parking.

    The mic gate silently drops audio, so a wrong expectation here means no
    frame ever arrives; a blocking read would hang the suite instead of
    reporting it.
    """
    frames: list[dict] = []
    arrived = threading.Event()

    def _read() -> None:
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] == until:
                arrived.set()
                return

    threading.Thread(target=_read, daemon=True).start()
    assert arrived.wait(timeout), (
        f"no {until!r} within {timeout}s; saw {[f['type'] for f in frames]}"
    )
    return frames


def test_a_spoken_utterance_runs_a_turn_and_is_spoken_back(app):
    with TestClient(app).websocket_connect("/ws/converse?origin=voice") as ws:
        assert ws.receive_json()["type"] == "hello"

        # A bare onset only lights the listening indicator; it starts nothing.
        ws.send_json(_audio("START"))
        assert ws.receive_json()["type"] == "speech_started"

        ws.send_json(_audio("END"))
        frames = _collect_until(ws, "turn_end")

    kinds = [f["type"] for f in frames]
    transcript = next(f for f in frames if f["type"] == "transcript")
    assert transcript["text"] == "hello agent"
    # The reply is streamed as audio while the model is still writing.
    assert "speaking_start" in kinds
    assert "audio_chunk" in kinds
    assert "speaking_end" in kinds
    assert "final" in kinds
    assert kinds[-1] == "turn_end"

    chunk = next(f for f in frames if f["type"] == "audio_chunk")
    assert chunk["sample_rate"] == 22050
    assert base64.b64decode(chunk["pcm"])


def test_an_empty_transcript_releases_the_ui_without_a_turn(app):
    """Silence must not run a turn, but must not leave the client waiting."""
    with TestClient(app).websocket_connect("/ws/converse?origin=voice") as ws:
        ws.receive_json()  # hello
        ws.send_json(_audio("ENDEMPTY"))
        frames = _collect_until(ws, "turn_end")

    kinds = [f["type"] for f in frames]
    assert "transcript" not in kinds
    assert "turn_start" not in kinds
    assert kinds == ["turn_end"]


def test_a_low_confidence_transcript_asks_the_user_to_repeat(app):
    """Garbled ASR gets a clarify frame rather than a bad query to the agent."""
    with TestClient(app).websocket_connect("/ws/converse?origin=voice") as ws:
        ws.receive_json()  # hello
        ws.send_json(_audio("ENDUNSURE"))
        frames = _collect_until(ws, "turn_end")

    kinds = [f["type"] for f in frames]
    assert "turn_start" not in kinds
    clarify = next(f for f in frames if f["type"] == "clarify")
    assert clarify["reason"] == "low_confidence"
    assert clarify["heard"] == "welcome to the general"


@pytest.mark.slow
def test_the_mic_is_ignored_while_a_turn_is_running(app):
    """With barge-in off, the answer is always heard in full.

    Mic input is dropped for the whole duration of an active turn — the agent's
    own TTS echo would otherwise cut its reply off or queue a spurious turn.
    """
    with TestClient(app).websocket_connect("/ws/converse?origin=voice") as ws:
        ws.receive_json()  # hello
        ws.send_json(_audio("ENDSLOW"))
        # Wait until the turn is demonstrably running before talking over it.
        assert _collect_until(ws, "turn_start")[-1]["type"] == "turn_start"

        ws.send_json(_audio("START"))
        frames = _collect_until(ws, "turn_end")

    # The onset was swallowed by the gate: no listening indicator, and no
    # second turn queued behind this one.
    assert "speech_started" not in [f["type"] for f in frames]


def test_audio_end_flushes_the_tail_as_an_utterance(app):
    """Push-to-talk release: whatever the VAD still holds becomes the turn.

    A frame of audio has to arrive first: the pipeline is built lazily on the
    first ``audio`` frame, so ``audio_end`` before any audio has nothing to
    flush and is silently a no-op.
    """
    with TestClient(app).websocket_connect("/ws/converse?origin=voice") as ws:
        ws.receive_json()  # hello
        ws.send_json(_audio("QUIET"))  # builds the pipeline; yields no utterance
        ws.send_json({"type": "audio_end"})
        frames = _collect_until(ws, "turn_end")

    transcript = next(f for f in frames if f["type"] == "transcript")
    assert transcript["text"] == "hello agent"
