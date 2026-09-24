"""ElevenLabs TTS writes the audio once, at the path it was asked for.

Both callers — `voice.synth.synthesize_pcm` and the announcer — read the path
they passed and nothing else, so a copy beside it is a second write nobody
reads. The request itself goes through a stand-in `httpx.AsyncClient`: no call
leaves the machine.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from yuyutsava.voice.backends import tts

MP3 = b"ID3\x04fake-mp3-bytes"


class _Response:
    content = MP3

    def raise_for_status(self) -> None:
        pass


class _Client:
    posts: list[dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def post(self, url: str, **kwargs: Any) -> _Response:
        _Client.posts.append({"url": url, **kwargs})
        return _Response()


def _synthesize(out: Path) -> None:
    engine = tts.ElevenLabsTTS(api_key="k", voice_id="v1")
    with mock.patch.object(tts.httpx, "AsyncClient", _Client):
        asyncio.run(asyncio.wait_for(engine.synthesize("hello", out), 10))


class WritesOnce(unittest.TestCase):
    def setUp(self) -> None:
        _Client.posts = []

    def test_a_wav_named_path_gets_the_bytes_and_nothing_else_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "sub" / "tts.wav"
            _synthesize(out)
            self.assertEqual(out.read_bytes(), MP3)
            self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ["tts.wav"])

    def test_an_mp3_named_path_gets_the_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "say.mp3"
            _synthesize(out)
            self.assertEqual(out.read_bytes(), MP3)
            self.assertEqual([p.name for p in Path(td).iterdir()], ["say.mp3"])

    def test_the_request_names_the_voice_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _synthesize(Path(td) / "x.wav")
        (post,) = _Client.posts
        self.assertTrue(post["url"].endswith("/text-to-speech/v1"))
        self.assertEqual(post["json"]["text"], "hello")
        self.assertEqual(post["headers"]["xi-api-key"], "k")


if __name__ == "__main__":
    unittest.main()
