"""The format speech is captured and processed in.

The microphone backend records at it, voice-activity detection frames at it,
the transcriber expects it and the earcons are generated at it — so these are
not four constants that happen to agree, they are one decision four modules
must share. `earcons.py` said so in a comment ("match the capture/playback
sample rate used elsewhere") while defining its own copy; a copy is how that
stops being true.

TTS output is *not* this. A synthesis engine writes at its own rate, and code
that writes synthesized audio passes that rate on rather than assuming this one.
"""

from __future__ import annotations

#: Speech capture, VAD, transcription and earcons.
SAMPLE_RATE = 16_000
#: Mono throughout.
CHANNELS = 1

__all__ = ["CHANNELS", "SAMPLE_RATE"]
