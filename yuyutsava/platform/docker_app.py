"""Starting Docker Desktop, which is the one OS-specific part of setting it up.

Everything else about containers is the same command on every OS (``docker
compose``). Launching the desktop application is not: it is an ``.exe`` under
Program Files on Windows, an ``.app`` on macOS, and on Linux there is usually
no application at all — the engine is a system service, so there is nothing to
launch and saying so is the honest answer.

Only known install locations are tried, and only ones that are there. Guessing
at a path and running whatever is at it is not something a setup wizard should
do on someone's machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

#: Where Docker Desktop installs itself, per OS, most likely first.
_WINDOWS = (
    r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
    r"C:\Program Files (x86)\Docker\Docker\Docker Desktop.exe",
)
_MACOS = ("/Applications/Docker.app",)


def docker_desktop_path() -> Path | None:
    """Where Docker Desktop is on this computer, or None if it is not found."""
    if sys.platform == "win32":
        candidates = [Path(path) for path in _WINDOWS]
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            candidates.append(Path(local) / "Docker" / "Docker Desktop.exe")
        return next((path for path in candidates if path.is_file()), None)
    if sys.platform == "darwin":
        return next((path for path in map(Path, _MACOS) if path.exists()), None)
    return None  # Linux runs the engine as a service; there is no app to open


def launch_command() -> list[str] | None:
    """The command that would start Docker Desktop, to show before running it."""
    found = docker_desktop_path()
    if found is None:
        return None
    if sys.platform == "darwin":
        return ["open", "-a", str(found)]
    return [str(found)]


def launch_docker_desktop() -> bool:
    """Start Docker Desktop and leave it running; whether it was started.

    Detached on purpose: the engine takes a while to come up, far longer than
    anything should wait on a subprocess, and whoever asked for this polls the
    engine itself to know when it is ready.
    """
    command = launch_command()
    if command is None:
        return False
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=sys.platform != "win32",
        )
    except OSError:
        return False
    return True
