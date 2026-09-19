"""What a captured utterance becomes — turn, queued follow-up, or nothing.

This logic used to live in closures inside `converse` and could only be reached
through a live socket, so the talk-over path in particular had no tests at all:
barge-in is off by default, which means the mic gate drops the very frames that
would exercise it. Extracted for `R-RDB-01`, it can now be driven directly.

The distinction under test is that silence means different things on the two
paths. A deliberate utterance that transcribes to nothing still has to release
the UI; a talk-over that transcribes to nothing is room noise or the agent's own
TTS echo, and the reply must be left to finish.
"""

from __future__ import annotations

import asyncio
import unittest

from yuyutsava.daemon.web.services.converse_session import MicDeps, UtteranceRouter


class _Transcript:
    def __init__(self, text: str, confidence: float | None) -> None:
        self.text = text
        self.confidence = confidence


class _FakeVoice:
    """Transcribes marker bytes into canned results."""

    def __init__(self) -> None:
        self.seen: list[bytes] = []

    async def transcribe_detailed(self, pcm: bytes) -> _Transcript:
        self.seen.append(pcm)
        if pcm == b"EMPTY":
            return _Transcript("", 0.9)
        if pcm == b"BLANK":
            return _Transcript("   ", 0.9)
        if pcm == b"UNSURE":
            return _Transcript("welcome to the general", 0.05)
        if pcm == b"BOOM":
            raise RuntimeError("stt exploded")
        return _Transcript("turn the lights on", 0.9)


class _Recorder:
    """Stands in for the socket: records sends, turns and persisted rows."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.turns: list[tuple[str, bool]] = []
        self.persisted: list[str] = []
        self.tasks: list[asyncio.Task] = []

    async def send(self, obj: dict) -> None:
        self.sent.append(obj)

    async def start_turn(self, text: str, *, preempt: bool = False) -> None:
        self.turns.append((text, preempt))

    async def persist(self, *, role: str, modality: str, text: str, **kw) -> None:
        self.persisted.append(text)

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    def kinds(self) -> list[str]:
        return [f["type"] for f in self.sent]


def _router(rec: _Recorder, voice: _FakeVoice | None, *, barge_in: bool = False):
    return UtteranceRouter(
        MicDeps(
            voice_of=lambda: voice,
            send=rec.send,
            start_turn=rec.start_turn,
            persist=rec.persist,
            spawn=rec.spawn,
        ),
        origin="voice",
        min_confidence=0.35,
        barge_in_enabled=barge_in,
    )


class HearingAnUtterance(unittest.IsolatedAsyncioTestCase):
    async def test_confident_speech_runs_a_turn(self) -> None:
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice)
        router.hear(b"SPEECH")
        await asyncio.gather(*rec.tasks)

        self.assertEqual(rec.turns, [("turn the lights on", False)])
        self.assertEqual(rec.persisted, ["turn the lights on"])
        self.assertIn("transcript", rec.kinds())

    async def test_silence_releases_the_ui_without_a_turn(self) -> None:
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice)
        router.hear(b"EMPTY")
        await asyncio.gather(*rec.tasks)

        self.assertEqual(rec.turns, [])
        self.assertEqual(rec.kinds(), ["turn_end"])

    async def test_a_garbled_transcript_asks_the_user_to_repeat(self) -> None:
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice)
        router.hear(b"UNSURE")
        await asyncio.gather(*rec.tasks)

        self.assertEqual(rec.turns, [], "garbled ASR must not reach the agent")
        self.assertEqual(rec.kinds(), ["clarify", "turn_end"])
        clarify = rec.sent[0]
        self.assertEqual(clarify["reason"], "low_confidence")
        self.assertEqual(clarify["heard"], "welcome to the general")

    async def test_a_whitespace_only_transcript_is_silence(self) -> None:
        """Whitespace is not a request — it must not reach the agent.

        The talk-over path already stripped before testing for emptiness; this
        path did not, so `"   "` ran a turn whose text was blank.
        """
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice)
        router.hear(b"BLANK")
        await asyncio.gather(*rec.tasks)

        self.assertEqual(rec.turns, [])
        self.assertEqual(rec.kinds(), ["turn_end"])

    async def test_a_newer_utterance_replaces_one_still_transcribing(self) -> None:
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice)
        router.hear(b"SPEECH")
        self.assertTrue(router.pending())
        router.hear(b"SPEECH")  # the stale one is cancelled
        await asyncio.gather(*rec.tasks, return_exceptions=True)

        self.assertEqual(len(rec.turns), 1, "only the latest utterance runs")


class QueueingWhileTheAgentAnswers(unittest.IsolatedAsyncioTestCase):
    async def test_a_queued_utterance_runs_when_the_turn_ends(self) -> None:
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice)
        router.queue(b"SPEECH")

        done: asyncio.Future = asyncio.get_running_loop().create_future()
        done.set_result(None)
        router.on_turn_done(object(), done)  # type: ignore[arg-type]
        await asyncio.gather(*rec.tasks)

        self.assertEqual(rec.turns, [("turn the lights on", False)])

    async def test_a_cancelled_turn_does_not_double_run(self) -> None:
        """A cancelled turn is a barge-in that already started a fresh turn."""
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice)
        router.queue(b"SPEECH")

        task = asyncio.ensure_future(asyncio.sleep(3600))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        router.on_turn_done(object(), task)
        await asyncio.gather(*rec.tasks)

        self.assertEqual(rec.turns, [])

    async def test_the_latest_queued_utterance_wins(self) -> None:
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice)
        router.queue(b"EMPTY")
        router.queue(b"SPEECH")

        done: asyncio.Future = asyncio.get_running_loop().create_future()
        done.set_result(None)
        router.on_turn_done(object(), done)  # type: ignore[arg-type]
        await asyncio.gather(*rec.tasks)

        self.assertEqual(rec.turns, [("turn the lights on", False)])


class TalkingOverTheAgent(unittest.IsolatedAsyncioTestCase):
    """Only real, confident words may cut a spoken reply off."""

    async def test_confident_talk_over_preempts_the_reply(self) -> None:
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice, barge_in=True)
        await router.maybe_barge_in(b"SPEECH")

        self.assertEqual(rec.turns, [("turn the lights on", True)])
        # speech_started tells the client to drop the stale audio it is playing.
        self.assertEqual(rec.kinds(), ["speech_started", "transcript"])

    async def test_noise_is_ignored(self) -> None:
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice, barge_in=True)
        await router.maybe_barge_in(b"EMPTY")

        self.assertEqual(rec.turns, [])
        self.assertEqual(rec.sent, [], "the reply is left to finish")

    async def test_whitespace_only_talk_over_is_ignored(self) -> None:
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice, barge_in=True)
        await router.maybe_barge_in(b"BLANK")

        self.assertEqual(rec.turns, [])

    async def test_a_low_confidence_guess_is_ignored(self) -> None:
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice, barge_in=True)
        await router.maybe_barge_in(b"UNSURE")

        self.assertEqual(rec.turns, [], "echo that half-transcribes must not interrupt")
        self.assertEqual(rec.sent, [])

    async def test_it_does_nothing_when_barge_in_is_off(self) -> None:
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice, barge_in=False)
        await router.maybe_barge_in(b"SPEECH")

        self.assertEqual(rec.turns, [])
        self.assertEqual(voice.seen, [], "not even transcribed")

    async def test_a_failed_transcription_cannot_kill_the_reply(self) -> None:
        rec, voice = _Recorder(), _FakeVoice()
        router = _router(rec, voice, barge_in=True)
        await router.maybe_barge_in(b"BOOM")  # must not raise

        self.assertEqual(rec.turns, [])


if __name__ == "__main__":
    unittest.main()
