"""HostProfile — the "OS passport".

One cached snapshot of everything OS-specific the agents and the safety spine
need to know about the machine they live on:

* which OS family / version / arch this is;
* the canonical native shell and how to invoke it (PowerShell on Windows,
  bash on POSIX — never cmd.exe: PowerShell is the Windows admin surface);
* which package managers / service manager / elevation mechanism exist here;
* the system-critical path prefixes for zone classification (L5);
* a ``prompt_block()`` passport injected into agent system prompts so the
  model always knows which system it is speaking to and which command dialect
  to emit.

Detection runs once per process (``functools.lru_cache``) — everything here
is cheap (``shutil.which`` + ``platform`` module reads).
"""

from __future__ import annotations

import functools
import os
import platform as _platform
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"

# ---------------------------------------------------------------------------
# System-critical prefixes per OS family — the L5 zone lists.
# ---------------------------------------------------------------------------

_POSIX_CRITICAL_PREFIXES: tuple[str, ...] = (
    "/etc",
    "/sys",
    "/proc",
    "/dev",
    "/boot",
    "/root",
    "/usr/bin",
    "/usr/sbin",
    "/var/log",
)


def _windows_critical_prefixes() -> tuple[str, ...]:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    return (
        system_root,                                   # C:\Windows (System32, drivers, …)
        program_files,
        program_files_x86,
        os.path.join(program_data, "Microsoft"),       # service/defender state
        r"C:\$Recycle.Bin",
        r"C:\System Volume Information",
    )


@dataclass(frozen=True)
class HostProfile:
    """Immutable facts about the host, computed once at startup."""

    os_family: str          # "windows" | "macos" | "linux"
    os_version: str
    arch: str
    python_version: str
    python_path: str
    shell_kind: str         # "powershell" | "pwsh" | "bash" | "sh"
    shell_prefix: tuple[str, ...]   # argv prefix; command string is appended
    package_managers: tuple[str, ...]
    service_manager: str    # "scm" | "launchd" | "systemd" | "unknown"
    elevation_mechanism: str  # "uac" | "osascript-admin" | "pkexec/sudo"
    home_dir: str
    temp_dir: str
    system_critical_prefixes: tuple[str, ...] = field(default=())

    # -- convenience flags -------------------------------------------------
    @property
    def is_windows(self) -> bool:
        return self.os_family == "windows"

    @property
    def is_macos(self) -> bool:
        return self.os_family == "macos"

    @property
    def is_linux(self) -> bool:
        return self.os_family == "linux"

    # -- L3: native shell invocation ---------------------------------------
    def shell_command(self, command: str) -> list[str]:
        """Argv that runs *command* in this host's canonical native shell."""
        return [*self.shell_prefix, command]

    # -- L5: zone facts -----------------------------------------------------
    def temp_zone_prefixes(self) -> tuple[str, ...]:
        """Prefixes classified as scratch/sandbox territory."""
        prefixes = [tempfile.gettempdir()]
        if self.is_macos:
            # macOS per-user temp roots live under /var/folders (symlinked
            # through /private); tempfile only reports the current one.
            prefixes += ["/tmp", "/private/tmp", "/var/folders", "/private/var/folders"]
        elif not self.is_windows:
            prefixes += ["/tmp", "/var/tmp"]
        return tuple(dict.fromkeys(prefixes))

    # -- prompt passport -----------------------------------------------------
    def prompt_block(self) -> str:
        """The OS passport injected into agent system prompts."""
        managers = ", ".join(self.package_managers) or "none detected"
        if self.is_windows:
            dialect = (
                "Native shell commands run in PowerShell (NOT cmd.exe, NOT bash). "
                "Use PowerShell syntax and cmdlets; system administration goes "
                "through PowerShell/CLI equivalents (Get-Service, msiexec, "
                "Get-WinEvent, sfc, DISM, reg). Open things for the user with "
                "`explorer.exe` / `Start-Process`."
            )
        elif self.is_macos:
            dialect = (
                "Native shell commands run in bash. System administration uses "
                "macOS tools (launchctl, defaults, systemsetup, log show, "
                "diskutil). Open things for the user with `open`."
            )
        else:
            dialect = (
                "Native shell commands run in bash. System administration uses "
                "Linux tools (systemctl, journalctl, the package manager). "
                "Open things for the user with `xdg-open`."
            )
        return (
            "## Host system\n"
            f"OS: {self.os_family} {self.os_version} ({self.arch}) | "
            f"shell: {self.shell_kind} | package managers: {managers} | "
            f"service manager: {self.service_manager} | "
            f"temp dir: {self.temp_dir}\n"
            f"{dialect}\n"
            "Prefer the deterministic tr_* tools for known operations and "
            "tr_run_python (portable, identical on every OS) for custom logic; "
            "reserve native shell for genuinely OS-specific work."
        )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _detect_shell() -> tuple[str, tuple[str, ...]]:
    if _IS_WINDOWS:
        pwsh = shutil.which("pwsh")
        if pwsh:
            return "pwsh", (pwsh, "-NoProfile", "-NonInteractive", "-Command")
        ps = shutil.which("powershell") or "powershell.exe"
        return "powershell", (ps, "-NoProfile", "-NonInteractive", "-Command")
    bash = shutil.which("bash")
    if bash:
        return "bash", (bash, "-c")
    return "sh", ("/bin/sh", "-c")


def _detect_package_managers() -> tuple[str, ...]:
    if _IS_WINDOWS:
        candidates = ("winget", "choco", "scoop")
    elif _IS_MACOS:
        candidates = ("brew", "port")
    else:
        candidates = ("apt", "dnf", "yum", "pacman", "zypper", "apk", "snap", "flatpak")
    return tuple(c for c in candidates if shutil.which(c))


def _detect_service_manager() -> str:
    if _IS_WINDOWS:
        return "scm"
    if _IS_MACOS:
        return "launchd"
    if shutil.which("systemctl"):
        return "systemd"
    return "unknown"


@functools.lru_cache(maxsize=1)
def host_profile() -> HostProfile:
    """Build (once) and return the host's OS passport."""
    if _IS_WINDOWS:
        family, version, elevation = "windows", _platform.version(), "uac"
        critical = _windows_critical_prefixes()
    elif _IS_MACOS:
        family, version, elevation = "macos", _platform.mac_ver()[0], "osascript-admin"
        critical = _POSIX_CRITICAL_PREFIXES
    else:
        family, version, elevation = "linux", _platform.release(), "pkexec/sudo"
        critical = _POSIX_CRITICAL_PREFIXES

    shell_kind, shell_prefix = _detect_shell()
    return HostProfile(
        os_family=family,
        os_version=version,
        arch=_platform.machine(),
        python_version=_platform.python_version(),
        python_path=sys.executable,
        shell_kind=shell_kind,
        shell_prefix=shell_prefix,
        package_managers=_detect_package_managers(),
        service_manager=_detect_service_manager(),
        elevation_mechanism=elevation,
        home_dir=str(Path.home()),
        temp_dir=tempfile.gettempdir(),
        system_critical_prefixes=critical,
    )
