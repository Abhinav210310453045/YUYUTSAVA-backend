"""Voice-activity segmentation: turn a stream of mic PCM into utterances.

The renderer streams 16 kHz mono int16 PCM frames over the WS. :class:`VadSegmenter`
buffers them into fixed-size frames, decides speech-vs-silence per frame, and
emits two things to the caller:

* ``speech_started`` — the first moment voiced audio is detected (used for the
  ``listening`` earcon and for barge-in: speech while the agent is talking).
* an ``utterance`` — the concatenated PCM of a speech segment, once enough
  trailing silence has elapsed (**auto-stop on silence**). That blob is what we
  hand to STT.

Backend: ``webrtcvad`` when installed (robust, cheap); otherwise a short-term
energy gate so the pipeline still works (degraded) without the optional dep.
A small pre-roll ring keeps the ~300 ms before speech onset so STT doesn't lose
the first phoneme.
"""

from __future__ import annotations

import collections
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("yuyutsava.audio_io.vad")

_SAMPLE_RATE = 16_000
_BYTES_PER_SAMPLE = 2


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass
class VadResult:
    """What :meth:`VadSegmenter.feed` observed for the bytes just fed."""

    speech_started: bool = False  # rising edge: silence -> speech
    utterance: bytes | None = None  # complete utterance PCM (set on silence after speech)


class VadSegmenter:
    """Segment a 16 kHz mono int16 PCM stream into utterances.

    Parameters
    ----------
    frame_ms:
        Frame size fed to the VAD (webrtcvad supports 10/20/30 ms only).
    aggressiveness:
        webrtcvad 0–3 (3 = most aggressively filters non-speech).
    start_frames:
        Consecutive speech frames required to declare speech started (debounce).
    silence_ms:
        Trailing silence that ends an utterance (auto-stop).
    preroll_ms:
        Audio kept before onset so the first phoneme isn't clipped.
    max_utterance_sec:
        Safety cap so a stuck-open mic still flushes.
    energy_threshold:
        RMS threshold for the fallback gate when webrtcvad is unavailable.
    min_utterance_ms:
        Minimum *voiced* duration for an utterance to be emitted. Shorter
        segments are dropped as noise blips — this is what stops a too-sensitive
        gate from flooding STT with sub-second fragments of ambient sound.
    barge_start_frames:
        Consecutive speech frames required to declare speech started **while the
        agent is speaking** (``set_speaking(True)``). Higher than ``start_frames``
        so a brief blip of the agent's own TTS echo can't self-interrupt.
    barge_energy_threshold:
        RMS floor a frame must clear to count as speech **while the agent is
        speaking**. Set comfortably above residual echo (post browser AEC) so
        only a real user talking over the agent triggers barge-in. Applied on
        top of webrtcvad when present, so barge-in needs loud *and* voiced audio.
    """

    @classmethod
    def from_env(cls) -> "VadSegmenter":
        """Build a segmenter whose thresholds come from the environment.

        Lets a user with a noisy room (fan, AC, keyboard) raise the gates without
        a code change. All knobs are optional; unset ones use the defaults below.

          YUYUTSAVA_VAD_AGGRESSIVENESS   webrtcvad 0–3 (higher = filters more noise)
          YUYUTSAVA_VAD_ENERGY           RMS gate when idle (fallback / pre-trigger)
          YUYUTSAVA_VAD_BARGE_ENERGY     RMS a frame must clear to interrupt the
                                         agent while it is speaking — raise this if
                                         fan/AC noise cuts off playback
          YUYUTSAVA_VAD_BARGE_FRAMES     consecutive loud+voiced frames needed to
                                         barge in (×30 ms) — raise for more debounce
          YUYUTSAVA_VAD_SILENCE_MS       trailing silence that ends an utterance
          YUYUTSAVA_VAD_MIN_UTTERANCE_MS minimum voiced audio to count as a turn
        """
        return cls(
            aggressiveness=_env_int("YUYUTSAVA_VAD_AGGRESSIVENESS", 2),
            energy_threshold=_env_float("YUYUTSAVA_VAD_ENERGY", 1000.0),
            barge_energy_threshold=_env_float("YUYUTSAVA_VAD_BARGE_ENERGY", 4000.0),
            barge_start_frames=_env_int("YUYUTSAVA_VAD_BARGE_FRAMES", 15),
            # Conversational pause window: a hands-free voice session stays
            # listening through natural mid-thought pauses and only ends the
            # utterance after ~2 s of continuous silence, so the user isn't
            # chopped off and fed to the agent in mid-sentence fragments. This
            # is deliberately generous — the common complaint is being cut off
            # while still thinking/speaking, and the cost of waiting a beat
            # longer after a real stop is far smaller than losing the tail of a
            # sentence. Raise it (YUYUTSAVA_VAD_SILENCE_MS) for even more
            # forgiving pauses, lower it for snappier push-to-talk turns.
            silence_ms=_env_int("YUYUTSAVA_VAD_SILENCE_MS", 2000),
            min_utterance_ms=_env_int("YUYUTSAVA_VAD_MIN_UTTERANCE_MS", 300),
        )

    def __init__(
        self,
        *,
        frame_ms: int = 30,
        aggressiveness: int = 2,
        start_frames: int = 3,
        silence_ms: int = 700,
        preroll_ms: int = 300,
        max_utterance_sec: float = 20.0,
        energy_threshold: float = 1000.0,
        min_utterance_ms: int = 300,
        # Defaults raised so ambient room noise (fans, AC) can't self-interrupt
        # the agent's own speech. Real talk-over still clears these because a
        # human voice close to the mic is much louder than background hum.
        barge_start_frames: int = 15,
        barge_energy_threshold: float = 4000.0,
    ) -> None:
        self._frame_bytes = int(_SAMPLE_RATE * frame_ms / 1000) * _BYTES_PER_SAMPLE
        self._frame_ms = frame_ms
        self._start_frames = start_frames
        self._silence_frames = max(1, silence_ms // frame_ms)
        self._max_frames = int(max_utterance_sec * 1000 / frame_ms)
        self._energy_threshold = energy_threshold
        self._min_speech_frames = max(1, min_utterance_ms // frame_ms)
        self._barge_start_frames = max(1, barge_start_frames)
        self._barge_energy_threshold = barge_energy_threshold

        self._vad = self._make_vad(aggressiveness)
        self._buf = bytearray()  # leftover bytes not yet a full frame
        self._preroll: collections.deque[bytes] = collections.deque(
            maxlen=max(1, preroll_ms // frame_ms)
        )
        self._voiced: list[bytes] = []  # frames of the current utterance
        self._in_speech = False
        self._speech_run = 0  # consecutive speech frames (pre-trigger)
        self._silence_run = 0  # consecutive silence frames (in speech)
        self._speech_total = 0  # total speech frames in the current utterance
        self._speaking = False  # agent is talking → stricter onset gating

    @staticmethod
    def _make_vad(aggressiveness: int):
        try:
            import webrtcvad  # type: ignore
            return webrtcvad.Vad(aggressiveness)
        except Exception:  # noqa: BLE001 — optional dep / build issues
            logger.info("webrtcvad unavailable — using energy-gate VAD fallback")
            return None

    def _is_speech(self, frame: bytes) -> bool:
        if self._speaking:
            # Barge-in gate: while the agent is talking, a frame must clear a
            # higher RMS floor (above residual echo) AND — when available —
            # webrtcvad must agree it's voiced. This keeps the agent's own TTS
            # echo from self-interrupting while still letting a real user talk
            # over it.
            if self._rms(frame) < self._barge_energy_threshold:
                return False
            if self._vad is not None:
                try:
                    return self._vad.is_speech(frame, _SAMPLE_RATE)
                except Exception:  # noqa: BLE001 — bad frame size etc.
                    return True  # already cleared the loud-energy gate
            return True
        if self._vad is not None:
            try:
                return self._vad.is_speech(frame, _SAMPLE_RATE)
            except Exception:  # noqa: BLE001 — bad frame size etc.
                return self._rms(frame) >= self._energy_threshold
        return self._rms(frame) >= self._energy_threshold

    def _rms(self, frame: bytes) -> float:
        # RMS over int16 samples without numpy (keeps fallback dependency-free).
        import array
        samples = array.array("h")
        samples.frombytes(frame)
        if not samples:
            return 0.0
        acc = 0
        for s in samples:
            acc += s * s
        return (acc / len(samples)) ** 0.5

    def feed(self, pcm: bytes) -> VadResult:
        """Feed a chunk of PCM; return what changed (speech onset / utterance)."""
        result = VadResult()
        self._buf.extend(pcm)
        while len(self._buf) >= self._frame_bytes:
            frame = bytes(self._buf[: self._frame_bytes])
            del self._buf[: self._frame_bytes]
            self._process_frame(frame, result)
        return result

    def _process_frame(self, frame: bytes, result: VadResult) -> None:
        speech = self._is_speech(frame)

        if not self._in_speech:
            self._preroll.append(frame)
            if speech:
                self._speech_run += 1
                onset_frames = (
                    self._barge_start_frames if self._speaking else self._start_frames
                )
                if self._speech_run >= onset_frames:
                    # Onset confirmed — open an utterance with the pre-roll.
                    self._in_speech = True
                    self._silence_run = 0
                    self._speech_total = self._speech_run
                    self._voiced = list(self._preroll)
                    self._preroll.clear()
                    result.speech_started = True
            else:
                self._speech_run = 0
            return

        # In speech: accumulate and watch for trailing silence / cap.
        self._voiced.append(frame)
        if speech:
            self._silence_run = 0
            self._speech_total += 1
        else:
            self._silence_run += 1

        if self._silence_run >= self._silence_frames or len(self._voiced) >= self._max_frames:
            # Drop utterances that were mostly silence/noise — too little actual
            # voiced audio to be a real turn (prevents STT floods on blips).
            if self._speech_total >= self._min_speech_frames:
                result.utterance = b"".join(self._voiced)
            self._reset_speech()

    def _reset_speech(self) -> None:
        self._in_speech = False
        self._speech_run = 0
        self._silence_run = 0
        self._speech_total = 0
        self._voiced = []

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def set_speaking(self, speaking: bool) -> None:
        """Toggle agent-speaking mode (stricter onset gating against self-echo).

        Call ``set_speaking(True)`` when the agent's TTS starts and ``False`` when
        it ends. In speaking mode the onset needs more sustained, louder audio so
        the agent's own voice leaking into the mic can't trigger barge-in, while a
        real user talking over still does. A half-formed onset run is dropped on
        the transition so the mode change starts clean.
        """
        if speaking != self._speaking and not self._in_speech:
            self._speech_run = 0
        self._speaking = speaking

    def reset(self) -> None:
        """Drop all buffered audio/state (e.g. after barge-in cancellation)."""
        self._buf.clear()
        self._preroll.clear()
        self._reset_speech()

    def flush(self) -> bytes | None:
        """Force-close any in-progress utterance (e.g. client said audio_end)."""
        utt = None
        if self._in_speech and self._voiced and self._speech_total >= self._min_speech_frames:
            utt = b"".join(self._voiced)
        self._reset_speech()
        return utt
