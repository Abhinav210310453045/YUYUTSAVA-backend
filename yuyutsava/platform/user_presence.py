"""Asking the OS to confirm the owner is at this computer — before YUYUTSAVA changes a key.

Opt-in (``[security] require_user_presence`` in ``models.toml``). Each OS asks
in its own dialog, so the owner answers the way they unlock the machine:

* **macOS** — Touch ID, an Apple Watch or the login password, in one system
  dialog (LocalAuthentication; :mod:`.presence_macos`, run as its own process).
* **Windows** — Windows Hello: face, fingerprint or PIN (``UserConsentVerifier``
  through Windows PowerShell). Without Hello set up, there is no check here.
* **Linux** — a polkit dialog, where the desktop session runs a polkit agent
  (``pkexec``). polkit asks for an administrator's authentication by default:
  stronger than presence, and not available headless or over SSH.

It guards YUYUTSAVA's own surfaces — the wizards, the app, ``yuyutsava models``
— not the files: the owner can always open ``secrets.toml`` in an editor. Every
answer names the mechanism that gave it, for the record.

One confirmation is remembered for a few minutes in this process, so a setup
that stores several keys asks once.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from yuyutsava.platform.hostprofile import host_profile

#: How long one confirmation lasts in this process.
REMEMBER_SECONDS = 300.0

#: How long a dialog may wait for its answer (the macOS helper's own limit, and some).
_WAIT_SECONDS = 130.0

#: Exit statuses of a check run as its own process (see :mod:`.presence_macos`).
_CONFIRMED, _REFUSED, _UNAVAILABLE, _TIMED_OUT = 0, 1, 2, 3

#: Runs ``argv`` and returns its exit status; ``env`` adds variables. Replaceable in tests.
Run = Callable[..., int]


class PresenceNotConfirmed(PermissionError):
    """The owner's presence was asked for and not confirmed: refused, unanswered or impossible."""


@dataclass(frozen=True)
class Presence:
    """A confirmation, and what gave it."""

    mechanism: str


class Backend(Protocol):
    """One OS's way of asking."""

    #: What it is called where people see it: "Touch ID or your password".
    mechanism: str

    def available(self) -> bool:
        """Whether this machine can ask now."""
        ...

    def verify(self, reason: str) -> int:
        """Ask; an exit status: 0 confirmed, 1 refused, 2 not possible, 3 unanswered."""
        ...


def _run(argv: list[str], *, env: dict[str, str] | None = None, timeout: float) -> int:
    try:
        done = subprocess.run(
            argv,
            env={**os.environ, **(env or {})},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _TIMED_OUT
    except OSError:
        return _UNAVAILABLE
    return done.returncode


class MacOS:
    mechanism = "Touch ID or your password"

    def __init__(self, run: Run = _run) -> None:
        self._run = run
        self._argv = [sys.executable, "-m", "yuyutsava.platform.presence_macos"]

    def available(self) -> bool:
        return self._run([*self._argv, "--can"], timeout=30) == _CONFIRMED

    def verify(self, reason: str) -> int:
        return self._run([*self._argv, reason], timeout=_WAIT_SECONDS)


#: Windows PowerShell 5.1 (``powershell.exe``): it can await a WinRT operation;
#: PowerShell 7 cannot load these types. The reason arrives in an environment
#: variable, never in the script text.
_HELLO = r"""
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    $task.Result
  }
  $v = [Windows.Security.Credentials.UI.UserConsentVerifier,Windows.Security.Credentials.UI,ContentType=WindowsRuntime]
  $can = Await ($v::CheckAvailabilityAsync()) ([Windows.Security.Credentials.UI.UserConsentVerifierAvailability])
  if ($can -ne 'Available') { exit 2 }
  if ($env:YUYUTSAVA_PRESENCE_ONLY_ASK_IF_POSSIBLE -eq '1') { exit 0 }
  $r = Await ($v::RequestVerificationAsync($env:YUYUTSAVA_PRESENCE_REASON)) ([Windows.Security.Credentials.UI.UserConsentVerificationResult])
  if ($r -eq 'Verified') { exit 0 }
  if ($r -eq 'Canceled' -or $r -eq 'RetriesExhausted') { exit 1 }
  exit 2
} catch { exit 2 }
"""


class Windows:
    mechanism = "Windows Hello"

    def __init__(self, run: Run = _run) -> None:
        self._run = run
        self._argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _HELLO]

    def available(self) -> bool:
        if shutil.which("powershell.exe") is None:
            return False
        only = {"YUYUTSAVA_PRESENCE_ONLY_ASK_IF_POSSIBLE": "1"}
        return self._run(self._argv, env=only, timeout=30) == _CONFIRMED

    def verify(self, reason: str) -> int:
        return self._run(
            self._argv, env={"YUYUTSAVA_PRESENCE_REASON": reason}, timeout=_WAIT_SECONDS
        )


class Linux:
    mechanism = "polkit (an administrator's password)"

    def __init__(self, run: Run = _run) -> None:
        self._run = run

    def available(self) -> bool:
        has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        return has_display and shutil.which("pkexec") is not None

    def verify(self, reason: str) -> int:
        # --disable-internal-agent: a desktop agent's dialog or nothing — never
        # a password prompt in some terminal no one is looking at.
        status = self._run(
            ["pkexec", "--disable-internal-agent", "/bin/true"], timeout=_WAIT_SECONDS
        )
        if status == 0:
            return _CONFIRMED
        return _REFUSED if status == 126 else _UNAVAILABLE  # 127: no agent, or not allowed


class Nowhere:
    mechanism = "none"

    def available(self) -> bool:
        return False

    def verify(self, reason: str) -> int:
        return _UNAVAILABLE


def backend_for(os_family: str, run: Run = _run) -> Backend:
    """This OS family's way of asking: ``macos``, ``windows`` or ``linux``."""
    if os_family == "macos":
        return MacOS(run)
    if os_family == "windows":
        return Windows(run)
    if os_family == "linux":
        return Linux(run)
    return Nowhere()


class PresenceGate:
    """Asks once, then remembers the answer for :data:`REMEMBER_SECONDS` in this process."""

    def __init__(
        self,
        backend: Backend,
        *,
        remember_seconds: float = REMEMBER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._remember = remember_seconds
        self._clock = clock
        self._confirmed_at: float | None = None
        self._available: bool | None = None
        self._lock = threading.Lock()

    @property
    def mechanism(self) -> str | None:
        """What would ask on this machine, or ``None`` when nothing can. Blocking the first time."""
        with self._lock:
            if self._available is None:
                self._available = self._backend.available()
            return self._backend.mechanism if self._available else None

    def confirm(self, reason: str) -> Presence:
        """Ask the OS to confirm the owner is here, saying *reason* in its dialog.

        *reason* completes "YUYUTSAVA is trying to …" on macOS, so say what is
        being done: "change the key secret:groq". Blocking: the dialog waits for
        an answer. Raises :class:`PresenceNotConfirmed` with what happened.
        """
        with self._lock:
            now = self._clock()
            if self._confirmed_at is not None and now - self._confirmed_at < self._remember:
                return Presence(self._backend.mechanism)
            status = self._backend.verify(reason)
            if status == _CONFIRMED:
                self._confirmed_at = self._clock()
                self._available = True
                return Presence(self._backend.mechanism)
        if status == _REFUSED:
            raise PresenceNotConfirmed(f"{self._backend.mechanism} was cancelled or refused")
        if status == _TIMED_OUT:
            raise PresenceNotConfirmed(f"{self._backend.mechanism} was not answered in time")
        raise PresenceNotConfirmed("this computer has no way to confirm it is you right now")


@functools.cache
def presence_gate() -> PresenceGate:
    """This process's gate, for this OS: one confirmation serves every caller."""
    return PresenceGate(backend_for(host_profile().os_family))


__all__ = [
    "REMEMBER_SECONDS",
    "Backend",
    "Linux",
    "MacOS",
    "Nowhere",
    "Presence",
    "PresenceGate",
    "PresenceNotConfirmed",
    "Windows",
    "backend_for",
    "presence_gate",
]
