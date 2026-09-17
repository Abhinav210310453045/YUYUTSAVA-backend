"""Everything the voice surface needs: the backends, and the pipeline on top.

Two layers, one package:

* :mod:`~yuyutsava.voice.backends` — the adapters that touch hardware and
  models: audio capture and playback, speech-to-text, text-to-speech, wake-word
  detection.
* the pipeline built on them — :mod:`~yuyutsava.voice.vad` decides where an
  utterance ends, :mod:`~yuyutsava.voice.sentence` cuts a reply into speakable
  pieces, :mod:`~yuyutsava.voice.synth` renders PCM for a remote client, and
  :mod:`~yuyutsava.voice.announcer` is the one serialized queue that speaks and
  plays earcons (:mod:`~yuyutsava.voice.earcons`) on the daemon host, so
  overlapping callers never talk over each other.

This was two packages — ``io/`` for the adapters and ``audio_io/`` for the
pipeline — split along a line documented nowhere, and ``io`` shadowed the
standard library's module of that name. The voice agent, the notification system
and the proposal channel all speak through the same announcer, which is the
reason to keep both layers in one place (R-PLC-07).

The Electron and mobile renderers keep their own client-side sound layer
(``electron-app/src/renderer/audio/``), because the daemon may be remote; both
ends expose the same earcon vocabulary.

``Announcer`` is re-exported because other modules import it from here; import
anything else from the module that owns it.
"""

from __future__ import annotations

from yuyutsava.voice.announcer import Announcer

__all__ = ["Announcer"]
