"""The wake-word subprocess's listen loop: what it emits for what it hears.

A wake is emitted the moment the detector fires (then the detector is reset
and listening resumes); five detector failures in a row end the process with
an error line. Driven with a fake microphone stream and detector.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from typing import Any

from yuyutsava.events.sources import _voice_proc as vp


class _Stream:
    def __init__(self, stopping: dict, chunks: int) -> None:
        self.stopping, self.left = stopping, chunks

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self, frames: int) -> tuple[bytes, bool]:
        self.left -= 1
        if self.left <= 0:
            self.stopping["v"] = True
        return b"\x00" * 4, False


class _SD:
    def __init__(self, stopping: dict, chunks: int) -> None:
        self.stopping, self.chunks = stopping, chunks

    def RawInputStream(self, **kw: Any) -> _Stream:  # noqa: N802 — sounddevice's name
        return _Stream(self.stopping, self.chunks)


class _Detector:
    def __init__(self, script: list) -> None:
        self.script, self.resets = list(script), 0

    def process(self, chunk: bytes) -> Any:
        item = self.script.pop(0) if self.script else None
        if isinstance(item, Exception):
            raise item
        return item

    def reset(self) -> None:
        self.resets += 1


def _listen(script: list, chunks: int) -> tuple[list[dict], _Detector]:
    stopping = {"v": False}
    det = _Detector(script)
    out = io.StringIO()
    with redirect_stdout(out):
        try:
            vp._listen(_SD(stopping, chunks), det, 16000, stopping)
        except SystemExit:
            pass
    return [json.loads(line) for line in out.getvalue().splitlines()], det


class Wake(unittest.TestCase):
    def test_a_wake_is_emitted_and_listening_resumes(self) -> None:
        lines, det = _listen([None, "hey_jarvis", None, "hey_jarvis"], chunks=4)
        wakes = [line for line in lines if line["kind"] == "wake"]
        self.assertEqual([w["wake_word"] for w in wakes], ["hey_jarvis", "hey_jarvis"])
        self.assertEqual(
            {k: wakes[0][k] for k in ("transcript", "blob_path", "duration_sec")},
            {"transcript": "", "blob_path": None, "duration_sec": 0.0},
        )
        self.assertEqual(det.resets, 2)

    def test_occasional_errors_are_tolerated(self) -> None:
        lines, _ = _listen([RuntimeError("x"), None, RuntimeError("y"), "w"], chunks=4)
        self.assertEqual([line["kind"] for line in lines], ["wake"])

    def test_five_failures_in_a_row_end_the_process(self) -> None:
        lines, _ = _listen([RuntimeError("model missing")] * 5, chunks=10)
        self.assertEqual(lines[-1]["kind"], "error")
        self.assertIn("wake detector unusable", lines[-1]["msg"])


if __name__ == "__main__":
    unittest.main()
