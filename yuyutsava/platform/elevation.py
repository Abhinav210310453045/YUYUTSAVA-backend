"""Per-operation privilege elevation — a standalone, reusable module.

The daemon runs as a normal user. When a task genuinely needs admin/root
rights (install an ``.msi``, restart a protected service, ``sfc /scannow``),
it asks the OS to elevate *that one command* through the native mechanism:

* Windows → ``Start-Process -Verb RunAs`` (UAC prompt)
* macOS   → ``osascript … with administrator privileges``
* Linux   → ``pkexec`` (GUI) or ``sudo``

Design goals (per the warden architecture doc):

* **Tool-agnostic.** ``tr_execute(elevated=True)`` is only the first consumer.
  Any future subsystem (installer flows, self-update, a helper service) imports
  :func:`get_elevation_provider` and calls ``run_elevated`` — no TaskRunner
  coupling.
* **Backend-swappable.** Every OS impl satisfies :class:`ElevationProvider`.
  A future persistent privileged helper (deferred Phase 2) can implement the
  same interface behind hardened IPC without touching callers.
* **Auditable.** Every elevated run returns an :class:`ElevationResult`
  carrying the mechanism used, so the consent/audit trail is uniform.

Elevation is always classified CRITICAL by the caller (fresh consent every
time, never cached) — this module only performs the mechanics.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from yuyutsava.platform.hostprofile import host_profile

logger = logging.getLogger("yuyutsava.platform.elevation")


@dataclass(frozen=True)
class ElevationResult:
    """Outcome of one elevated command — shaped for audit + ShellResult mapping."""

    stdout: str
    stderr: str
    exit_code: int
    mechanism: str  # e.g. "uac", "osascript-admin", "pkexec", "sudo"


class ElevationProvider(Protocol):
    """One privileged-execution backend for the current host."""

    mechanism_name: str

    def is_elevated(self) -> bool:
        """True if THIS process already runs with admin/root rights."""
        ...

    async def run_elevated(self, command: str, *, timeout: int = 300) -> ElevationResult:
        """Run *command* elevated in the host's native shell; capture output."""
        ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _run_capture(argv: list[str], *, timeout: int) -> tuple[str, str, int]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise
    return (
        out.decode(errors="replace").strip(),
        err.decode(errors="replace").strip(),
        proc.returncode if proc.returncode is not None else -1,
    )


# ---------------------------------------------------------------------------
# Windows — UAC via Start-Process -Verb RunAs
# ---------------------------------------------------------------------------


class WindowsUACProvider:
    mechanism_name = "uac"

    def is_elevated(self) -> bool:
        try:
            import ctypes  # noqa: PLC0415 - win-only, imported lazily

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    async def run_elevated(self, command: str, *, timeout: int = 300) -> ElevationResult:
        # A RunAs child cannot share our pipes (it is a separate elevated
        # process), so redirect its stdout/stderr to temp files inside the
        # elevated shell, then read them back here.
        tmp = Path(tempfile.mkdtemp(prefix="yuyutsava_elev_"))
        out_f, err_f = tmp / "out.txt", tmp / "err.txt"
        # Inner command runs in a nested PowerShell whose streams are file-redirected.
        inner = (
            f"{command} "
            f"1> '{out_f}' 2> '{err_f}'"
        )
        b64 = _encode_powershell(inner)
        # Outer PowerShell launches the elevated child and waits for it.
        launcher = (
            "$p = Start-Process powershell "
            "-Verb RunAs -Wait -WindowStyle Hidden "
            f"-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{b64}' "
            "-PassThru; exit $p.ExitCode"
        )
        argv = [
            _powershell_exe(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            launcher,
        ]
        try:
            _, launch_err, code = await _run_capture(argv, timeout=timeout)
        finally:
            out = _read_text(out_f)
            err = _read_text(err_f)
            _cleanup(tmp)
        # UAC declined / launcher failure surfaces on the launcher's stderr.
        if launch_err and not err:
            err = launch_err
        return ElevationResult(stdout=out, stderr=err, exit_code=code, mechanism=self.mechanism_name)


def _powershell_exe() -> str:
    import shutil  # noqa: PLC0415

    return shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"


def _encode_powershell(command: str) -> str:
    """PowerShell -EncodedCommand wants base64 of UTF-16LE — dodges all quoting."""
    import base64  # noqa: PLC0415

    return base64.b64encode(command.encode("utf-16-le")).decode("ascii")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _cleanup(tmp: Path) -> None:
    import shutil  # noqa: PLC0415

    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# macOS — osascript "with administrator privileges"
# ---------------------------------------------------------------------------


class MacAdminProvider:
    mechanism_name = "osascript-admin"

    def is_elevated(self) -> bool:
        return _posix_is_root()

    async def run_elevated(self, command: str, *, timeout: int = 300) -> ElevationResult:
        # `do shell script` already runs the command and returns its stdout;
        # it raises on non-zero exit, which osascript reports on stderr.
        script = (
            'do shell script "' + _applescript_quote(command) + '" '
            "with administrator privileges"
        )
        out, err, code = await _run_capture(
            ["osascript", "-e", script], timeout=timeout
        )
        return ElevationResult(stdout=out, stderr=err, exit_code=code, mechanism=self.mechanism_name)


def _applescript_quote(command: str) -> str:
    # Inside an AppleScript double-quoted string: escape backslashes then quotes.
    return command.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Linux — pkexec (preferred, GUI prompt) else sudo
# ---------------------------------------------------------------------------


class LinuxSudoProvider:
    def __init__(self) -> None:
        import shutil  # noqa: PLC0415

        self._pkexec = shutil.which("pkexec")
        self._sudo = shutil.which("sudo")
        self.mechanism_name = "pkexec" if self._pkexec else "sudo"

    def is_elevated(self) -> bool:
        return _posix_is_root()

    async def run_elevated(self, command: str, *, timeout: int = 300) -> ElevationResult:
        profile = host_profile()
        shell_argv = profile.shell_command(command)  # e.g. ["/bin/bash", "-c", command]
        if self._pkexec:
            argv = [self._pkexec, *shell_argv]
        elif self._sudo:
            # -n: never prompt on a TTY-less daemon; fail fast if no cached creds.
            argv = [self._sudo, "-n", *shell_argv]
        else:
            return ElevationResult(
                stdout="",
                stderr="no elevation mechanism available (need pkexec or sudo)",
                exit_code=127,
                mechanism="none",
            )
        out, err, code = await _run_capture(argv, timeout=timeout)
        return ElevationResult(stdout=out, stderr=err, exit_code=code, mechanism=self.mechanism_name)


def _posix_is_root() -> bool:
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except AttributeError:
        return False


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def get_elevation_provider() -> ElevationProvider:
    """Return the elevation backend for this host."""
    if sys.platform == "win32":
        return WindowsUACProvider()
    if sys.platform == "darwin":
        return MacAdminProvider()
    return LinuxSudoProvider()
