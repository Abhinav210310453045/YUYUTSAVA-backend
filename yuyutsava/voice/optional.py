"""The voice extra's packages, imported on first use.

Everything audio — capture and playback, speech-to-text, the wake word, codec
work — sits behind ``uv pip install 'yuyutsava[voice]'`` (plus ``groq`` for the
hosted STT). Each backend imports its package through :func:`require`, so a
missing one always surfaces as the same :class:`VoiceDependencyMissing` with an
install hint, and a caller that can go quiet without audio catches one type.
"""

from __future__ import annotations

import importlib
from types import ModuleType

INSTALL_HINT = "run: uv pip install 'yuyutsava[voice]'"


class VoiceDependencyMissing(RuntimeError):
    """An optional voice package is not installed."""


def require(module: str, message: str) -> ModuleType:
    """Import *module*, or raise :class:`VoiceDependencyMissing` with *message*."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise VoiceDependencyMissing(message) from exc
