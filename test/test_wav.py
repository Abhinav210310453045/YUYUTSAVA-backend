"""Writing speech and synthesized audio to disk, and why the rate is required.

`R-DRY-02` (W4): five modules wrote the same five lines of `wave` setup and four
defined their own 16 kHz. One writer and one constant now.

The rate is a required argument to the writer, on purpose. Speech is captured
at 16 kHz, but a TTS engine writes at its own rate — and audio written at the
wrong rate raises nothing: it just plays back at the wrong pitch and speed.
A convenient default is exactly how that happens silently.
"""

from __future__ import annotations

import inspect
import pathlib
import re
import tempfile
import unittest
import wave

from yuyutsava.voice import pcm
from yuyutsava.wav import SAMPLE_WIDTH, write_pcm16

ROOT = pathlib.Path(__file__).resolve().parent.parent / "yuyutsava"


def _read(path: pathlib.Path) -> tuple[int, int, int, bytes]:
    with wave.open(str(path), "rb") as wf:
        return wf.getframerate(), wf.getnchannels(), wf.getsampwidth(), wf.readframes(10**6)


class TheWriter(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = pathlib.Path(tempfile.mkdtemp())

    def test_what_is_written_reads_back(self) -> None:
        frames = bytes(range(0, 200, 2)) * 4
        path = write_pcm16(self.dir / "a.wav", frames, sample_rate=16_000)
        rate, channels, width, data = _read(path)
        self.assertEqual((rate, channels, width), (16_000, 1, SAMPLE_WIDTH))
        self.assertEqual(data, frames)

    def test_the_rate_is_the_caller_s(self) -> None:
        """A TTS engine's 22.05 kHz must survive untouched."""
        path = write_pcm16(self.dir / "tts.wav", b"\x00\x00" * 100, sample_rate=22_050)
        self.assertEqual(_read(path)[0], 22_050)

    def test_the_rate_has_no_default(self) -> None:
        """Deliberately: see the module docstring."""
        param = inspect.signature(write_pcm16).parameters["sample_rate"]
        self.assertIs(param.default, inspect.Parameter.empty)
        with self.assertRaises(TypeError):
            write_pcm16(self.dir / "x.wav", b"")  # type: ignore[call-arg]

    def test_the_directory_is_made(self) -> None:
        path = write_pcm16(self.dir / "deep" / "er" / "a.wav", b"\x00\x00", sample_rate=8_000)
        self.assertTrue(path.exists())

    def test_stereo_is_honoured(self) -> None:
        path = write_pcm16(self.dir / "s.wav", b"\x00\x00" * 4, sample_rate=16_000, channels=2)
        self.assertEqual(_read(path)[1], 2)


class OneSpeechFormat(unittest.TestCase):
    def test_speech_is_sixteen_kilohertz_mono(self) -> None:
        self.assertEqual((pcm.SAMPLE_RATE, pcm.CHANNELS), (16_000, 1))

    def test_nobody_defines_their_own(self) -> None:
        """Four modules had to agree on this and each held a copy; one of them
        said so in a comment while keeping it."""
        pattern = re.compile(r"^\s*_?SAMPLE_RATE\s*=\s*\d", re.M)
        found = [
            str(p.relative_to(ROOT.parent))
            for p in sorted(ROOT.rglob("*.py"))
            if pattern.search(p.read_text())
        ]
        self.assertEqual(found, ["yuyutsava/voice/pcm.py"])

    def test_nobody_writes_a_wav_by_hand(self) -> None:
        found = [
            str(p.relative_to(ROOT.parent))
            for p in sorted(ROOT.rglob("*.py"))
            if p.name != "wav.py" and re.search(r"wave\.open\([^)]*[\"']wb[\"']", p.read_text())
        ]
        self.assertEqual(found, [])


class SynthesizedAudioKeepsItsOwnRate(unittest.TestCase):
    def test_the_board_s_audio_block_passes_the_engine_rate_on(self) -> None:
        """Checked in the source: running it needs a TTS engine. The call must
        use the rate synthesis returned, never the speech-capture constant."""
        source = (ROOT / "todoboard" / "block_audio.py").read_text()
        call = re.search(r"write_pcm16\((.*?)\)\n", source, re.S)
        self.assertIsNotNone(call, "block_audio no longer writes through write_pcm16")
        self.assertIn("sample_rate=rate", call.group(1))
        self.assertNotIn("SAMPLE_RATE", call.group(1))


if __name__ == "__main__":
    unittest.main()
