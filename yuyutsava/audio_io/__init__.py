"""Reusable, agent-agnostic sound layer for YUYUTSAVA (server side).

This package is the single choke-point for "make a sound" / "say something" on
the **daemon host**. It deliberately knows nothing about agents, conversations,
or proposals: the voice agent, the notification system, and the proposal channel
all call into the same :class:`~yuyutsava.audio_io.announcer.Announcer` so the
sound behaviour stays consistent and is defined in one place.

Two primitives:

* **earcons** — short, named UI sounds (``open``, ``close``, ``listening``,
  ``done``, ``error``) — see :mod:`yuyutsava.audio_io.earcons`.
* **the Announcer** — a small serialized queue that turns text into speech via
  the configured TTS backend and plays earcons, so overlapping callers never
  talk over each other — see :mod:`yuyutsava.audio_io.announcer`.

The Electron/mobile renderer has its own parallel sound layer
(``electron-app/src/renderer/audio/``) for client-side playback, since the
daemon may be remote (Tailscale); both expose the same earcon vocabulary.

``Announcer`` is re-exported because other modules import it from here; import
anything else from the module that owns it.
"""

from __future__ import annotations

from yuyutsava.audio_io.announcer import Announcer

__all__ = ["Announcer"]
