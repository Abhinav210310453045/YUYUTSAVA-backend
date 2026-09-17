"""Adapters that touch hardware and models, each behind a small factory.

Every module here reads its choice from the environment and returns something
the pipeline can use without knowing which implementation it got:

- :mod:`~yuyutsava.voice.backends.device` — PortAudio capture and playback; all
  audio I/O goes through it
- :mod:`~yuyutsava.voice.backends.stt` — speech-to-text (default:
  ``faster_whisper``, fully local)
- :mod:`~yuyutsava.voice.backends.tts` — text-to-speech (default: the ``piper``
  CLI, fully local)
- :mod:`~yuyutsava.voice.backends.wake` — wake-word detection (default:
  ``openwakeword``, local)

These carry the optional dependencies: ``uv pip install 'yuyutsava[voice]'``. A
missing extra surfaces when a backend is actually built, not at import, so the
CLI and the daemon start fine without them.

This package re-exports nothing: import a name from the module that owns it.
"""
