"""The user-presence gate: each OS asks its own way, an answer is remembered a few minutes,
and anything but a confirmation stops the change. No dialog is shown: each backend runs a
double in place of its process."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

from yuyutsava.platform.user_presence import (
    Linux,
    MacOS,
    Nowhere,
    PresenceGate,
    PresenceNotConfirmed,
    Windows,
    backend_for,
)


class _Runs:
    """Stands in for running a process: records each call, answers with *statuses* in turn."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, *, env=None, timeout):
        self.calls.append((argv, env or {}))
        return self.statuses.pop(0)


class _Backend:
    mechanism = "Touch ID or your password"

    def __init__(self, *statuses: int, available: bool = True) -> None:
        self.statuses = list(statuses)
        self.asked: list[str] = []
        self.probed = 0
        self._available = available

    def available(self) -> bool:
        self.probed += 1
        return self._available

    def verify(self, reason: str) -> int:
        self.asked.append(reason)
        return self.statuses.pop(0)


class EachOS(unittest.TestCase):
    def test_has_its_own_way_of_asking(self) -> None:
        self.assertIsInstance(backend_for("macos"), MacOS)
        self.assertIsInstance(backend_for("windows"), Windows)
        self.assertIsInstance(backend_for("linux"), Linux)
        self.assertIsInstance(backend_for("plan9"), Nowhere)

    def test_macos_asks_in_its_own_process_saying_why(self) -> None:
        runs = _Runs(0, 0)
        backend = MacOS(runs)
        self.assertTrue(backend.available())
        self.assertEqual(backend.verify("change the key secret:groq"), 0)
        (probe, _), (ask, _) = runs.calls
        self.assertEqual(
            probe, [sys.executable, "-m", "yuyutsava.platform.presence_macos", "--can"]
        )
        self.assertEqual(ask[-1], "change the key secret:groq")

    def test_windows_passes_the_reason_outside_the_script(self) -> None:
        runs = _Runs(0)
        Windows(runs).verify("change the key secret:groq")
        argv, env = runs.calls[0]
        self.assertEqual(argv[0], "powershell.exe")
        self.assertNotIn("secret:groq", " ".join(argv))
        self.assertEqual(env["YUYUTSAVA_PRESENCE_REASON"], "change the key secret:groq")

    def test_windows_without_powershell_cannot_ask(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            self.assertFalse(Windows(_Runs()).available())

    def test_linux_needs_a_desktop_and_reads_pkexecs_answer(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("shutil.which", return_value="/usr/bin/pkexec"),
        ):
            self.assertFalse(Linux(_Runs()).available())  # no display: no dialog to show
            os.environ["WAYLAND_DISPLAY"] = "wayland-0"
            self.assertTrue(Linux(_Runs()).available())
        self.assertEqual([Linux(_Runs(s)).verify("x") for s in (0, 126, 127)], [0, 1, 2])


class TheGate(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1000.0

    def gate(self, backend) -> PresenceGate:
        return PresenceGate(backend, remember_seconds=300, clock=lambda: self.now)

    def test_asks_once_then_remembers_for_a_while(self) -> None:
        backend = _Backend(0, 0)
        gate = self.gate(backend)
        self.assertEqual(gate.confirm("change a key").mechanism, "Touch ID or your password")
        self.now += 299
        gate.confirm("change another")
        self.assertEqual(backend.asked, ["change a key"])
        self.now += 2
        gate.confirm("and another")
        self.assertEqual(len(backend.asked), 2)

    def test_a_refusal_stops_the_change_and_is_not_remembered(self) -> None:
        backend = _Backend(1, 0)
        gate = self.gate(backend)
        with self.assertRaises(PresenceNotConfirmed) as caught:
            gate.confirm("change a key")
        self.assertIn("cancelled or refused", str(caught.exception))
        gate.confirm("change a key")
        self.assertEqual(len(backend.asked), 2)

    def test_no_answer_or_no_way_to_ask_stops_it_too(self) -> None:
        gate = self.gate(_Backend(3, 2))
        with self.assertRaises(PresenceNotConfirmed) as late:
            gate.confirm("x")
        self.assertIn("not answered in time", str(late.exception))
        with self.assertRaises(PresenceNotConfirmed) as impossible:
            gate.confirm("x")
        self.assertIn("no way to confirm", str(impossible.exception))

    def test_says_what_would_ask_and_probes_once(self) -> None:
        backend = _Backend()
        gate = self.gate(backend)
        self.assertEqual(gate.mechanism, "Touch ID or your password")
        self.assertEqual(gate.mechanism, "Touch ID or your password")
        self.assertEqual(backend.probed, 1)
        self.assertIsNone(self.gate(_Backend(available=False)).mechanism)


if __name__ == "__main__":
    unittest.main()
