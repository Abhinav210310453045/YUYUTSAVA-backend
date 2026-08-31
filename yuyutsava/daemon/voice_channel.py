"""VoiceChannel — UserChannel implementation backed by STT + TTS.

Responsibilities:
- ``post_event``: speaks brief "log" messages aloud via TTS.
- ``post_proposal``: reads the proposal text, then listens for a yes/no/skip
  response via STT. Falls back gracefully (raises ``NotImplementedError``)
  when STT is unavailable or the response is ambiguous, so the
  ``ChannelRouter`` falls through to the next channel (typically ``WebChannel``).
- ``post_ask``: speaks the question and captures the free-text response.

Voice is the *awareness* channel: great for "you have a pending proposal", not
ideal for rich editing.  Modify/skip_remember decisions always fall through to
the web channel.

The channel does NOT mute itself during TTS; add echo-cancellation logic here
if that becomes a real problem.

Architecture note: the voice *source* (wake-word → voice.wake event) is a
separate concern handled by ``VoiceSource`` in
``yuyutsava.events.sources.voice``.  ``VoiceChannel`` only handles the
*output* side (proposals → spoken audio) and the *response* side (STT for
yes/no/free-text answers).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path

from yuyutsava.audio_io import Announcer
from yuyutsava.daemon.channels import (
    AskPrompt, ChannelEvent, LogPayload, ProposalDecision, UserChannel,
)
from yuyutsava.storage.events import Proposal
from yuyutsava.io.audio import AudioUnavailableError, capture_wav
from yuyutsava.io.stt import STT

logger = logging.getLogger("yuyutsava.daemon.voice_channel")

# Coarse yes/no keyword lists — intentionally broad.
_YES_WORDS = frozenset({"yes", "yeah", "yep", "approve", "ok", "okay", "sure", "go"})
_NO_WORDS  = frozenset({"no", "nope", "skip", "cancel", "stop", "deny", "reject"})

# Duration of the capture window for short yes/no answers.
_DECISION_LISTEN_SEC = 5.0
# Duration for free-text ask responses.
_ASK_LISTEN_SEC = 10.0


def _words(text: str) -> set[str]:
    """Lower-cased word set."""
    return set(re.sub(r"[^a-z ]", "", text.lower()).split())


class VoiceChannel(UserChannel):
    """UserChannel that speaks via TTS and listens via STT."""

    name = "voice"

    def __init__(self, announcer: Announcer, stt: STT) -> None:
        # Output (speaking + earcons) goes through the shared Announcer so the
        # sound layer is decoupled and reusable; this channel only adds the STT
        # *input* side on top.
        self._announcer = announcer
        self._stt = stt
        self._tmp = Path(tempfile.mkdtemp(prefix="yuyutsava_voice_"))

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    async def _speak(self, text: str) -> None:
        """Speak ``text`` via the shared Announcer. Logs but never raises."""
        if not text.strip():
            return
        try:
            await self._announcer.say(text)
        except Exception:
            logger.warning("TTS speak failed", exc_info=True)

    async def _listen(self, duration_sec: float) -> str:
        """Capture audio and transcribe. Returns empty string on failure."""
        wav = self._tmp / f"capture_{asyncio.get_event_loop().time():.3f}.wav"
        try:
            await capture_wav(wav, duration_sec)
            return await self._stt.transcribe(wav)
        except AudioUnavailableError:
            logger.debug("audio unavailable for STT capture")
            return ""
        except Exception:
            logger.warning("STT listen failed", exc_info=True)
            return ""

    # ------------------------------------------------------------------ #
    # UserChannel interface                                                #
    # ------------------------------------------------------------------ #

    async def post_event(self, ev: ChannelEvent) -> None:
        if not isinstance(ev.payload, LogPayload):
            return
        msg = (ev.payload.text or "").strip()
        if not msg:
            return
        # Keep announcements short so voice doesn't become noisy.
        brief = msg[:120]
        await self._speak(brief)

    async def post_proposal(self, p: Proposal) -> ProposalDecision:
        """Read proposal aloud; listen for yes/no. Fall through on ambiguity."""
        prompt = (
            f"Proposal: {p.proposed}. "
            "Say yes to approve, or no to skip."
        )
        await self._speak(prompt)

        transcript = await self._listen(_DECISION_LISTEN_SEC)
        if not transcript:
            raise NotImplementedError("voice: no STT response captured")

        words = _words(transcript)
        if words & _YES_WORDS:
            logger.info("voice: approved proposal %s ('%s')", p.proposal_id, transcript)
            return ProposalDecision(decision="approve")
        if words & _NO_WORDS:
            logger.info("voice: skipped proposal %s ('%s')", p.proposal_id, transcript)
            return ProposalDecision(decision="skip")

        # Ambiguous — fall through to web channel.
        logger.info(
            "voice: could not parse response '%s' for proposal %s; deferring",
            transcript, p.proposal_id,
        )
        raise NotImplementedError(f"voice: ambiguous response {transcript!r}")

    async def post_ask(self, a: AskPrompt) -> str:
        """Read question aloud; return free-text transcript as the answer."""
        text = a.body
        if a.options:
            text += f" Options: {', '.join(a.options)}."
        await self._speak(text)

        transcript = await self._listen(_ASK_LISTEN_SEC)
        if not transcript:
            raise NotImplementedError("voice: no STT response for ask")

        logger.info("voice: ask %s answered: '%s'", a.ask_id, transcript)
        return transcript

    async def shutdown(self) -> None:
        try:
            await self._announcer.aclose()
        except Exception:
            pass
        try:
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def voice_channel_from_env() -> VoiceChannel:
    """Build a VoiceChannel from environment variables.

    STT_PROVIDER / TTS_PROVIDER select backends (default: faster_whisper / piper).
    Raises RuntimeError if required env vars (e.g. PIPER_MODEL) are absent — so a
    misconfigured voice channel fails fast at startup rather than going silent.
    """
    from yuyutsava.io.stt import stt_from_env
    from yuyutsava.io.tts import tts_from_env

    # Build TTS eagerly to validate config, then hand it to the Announcer.
    tts = tts_from_env()
    announcer = Announcer(tts_factory=lambda: tts)
    return VoiceChannel(announcer=announcer, stt=stt_from_env())
