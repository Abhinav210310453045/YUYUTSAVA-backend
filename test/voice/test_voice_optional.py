"""A missing voice package is one exception type, and the announcer goes quiet on it.

R-ERR-03: the device layer raised ``AudioUnavailableError`` while speech-to-text,
the wake word and codec work raised a bare ``RuntimeError`` with the same
install hint. The announcer caught only the first, so a TTS whose codec package
was missing logged a "playback failed" warning with a traceback on every
utterance instead of going silent the way a missing sound device does.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yuyutsava.voice.announcer import Announcer
from yuyutsava.voice.backends import device
from yuyutsava.voice.backends.stt import FasterWhisperSTT, GroqWhisperSTT
from yuyutsava.voice.backends.wake import OpenWakeWordDetector
from yuyutsava.voice.optional import VoiceDependencyMissing, require


def _without(module: str):
    """Make ``import <module>`` fail as if the package were not installed."""
    return mock.patch.dict(sys.modules, {module: None})


class Require(unittest.TestCase):
    def test_a_missing_package_raises_the_one_type_with_its_cause(self) -> None:
        with _without("sounddevice"), self.assertRaises(VoiceDependencyMissing) as ctx:
            require("sounddevice", "sounddevice not installed")
        self.assertIsInstance(ctx.exception, RuntimeError)
        self.assertIsInstance(ctx.exception.__cause__, ImportError)

    def test_an_installed_package_is_returned(self) -> None:
        self.assertIs(require("json", "unused"), sys.modules["json"])


class EveryBackendRaisesIt(unittest.TestCase):
    def test_audio_device(self) -> None:
        with _without("sounddevice"), self.assertRaisesRegex(VoiceDependencyMissing, "sounddevice"):
            device._require_sd()

    def test_local_speech_to_text(self) -> None:
        with (
            _without("faster_whisper"),
            self.assertRaisesRegex(VoiceDependencyMissing, "faster-whisper"),
        ):
            FasterWhisperSTT()._load()

    def test_hosted_speech_to_text(self) -> None:
        with _without("groq"), self.assertRaisesRegex(VoiceDependencyMissing, "groq"):
            asyncio.run(GroqWhisperSTT(api_key="k").transcribe(Path("/nonexistent.wav")))

    def test_wake_word(self) -> None:
        with (
            _without("openwakeword"),
            self.assertRaisesRegex(VoiceDependencyMissing, "openwakeword"),
        ):
            OpenWakeWordDetector()._load()


class _TTSWithoutItsCodec:
    async def synthesize(self, text: str, out: Path) -> None:
        raise VoiceDependencyMissing("non-WAV TTS output needs soundfile")


class TheAnnouncerGoesQuiet(unittest.IsolatedAsyncioTestCase):
    async def test_a_missing_codec_is_silent_not_a_warning_per_utterance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            announcer = Announcer(tts_factory=_TTSWithoutItsCodec, tmp_dir=Path(tmp))
            try:
                with self.assertNoLogs("yuyutsava", level="WARNING"):
                    await announcer.say("hello")
            finally:
                await announcer.aclose()


if __name__ == "__main__":
    unittest.main()
