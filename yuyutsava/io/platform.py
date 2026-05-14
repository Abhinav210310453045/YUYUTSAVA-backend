"""Platform detection and optional voice dependency checks."""

from __future__ import annotations

import platform


def is_macos() -> bool:
    return platform.system() == "Darwin"


def is_linux() -> bool:
    return platform.system() == "Linux"


def is_windows() -> bool:
    return platform.system() == "Windows"


def check_voice_deps() -> dict[str, bool]:
    """Return which voice-related packages are importable."""
    results: dict[str, bool] = {}
    for pkg in ("sounddevice", "soundfile", "openwakeword", "faster_whisper"):
        try:
            __import__(pkg)
            results[pkg] = True
        except ImportError:
            results[pkg] = False
    return results
