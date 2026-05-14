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
import shutil
import tempfile
from pathlib import Path

from yuyutsava.daemon.channels import (
    AskPrompt, ChannelEvent, ProposalDecision, UserChannel,
)
from yuyutsava.events.store import Proposal
from yuyutsava.io.audio import AudioUnavailableError, capture_wav, play_wav
from yuyutsava.io.stt import STT
from yuyutsava.io.tts import TTS

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
    import re
    return set(re.sub(r"[^a-z ]", "", text.lower()).split())


class VoiceChannel(UserChannel):
    """UserChannel that speaks via TTS and listens via STT."""

    name = "voice"

    def __init__(self, tts: TTS, stt: STT) -> None:
        self._tts = tts
        self._stt = stt
        self._tmp = Path(tempfile.mkdtemp(prefix="yuyutsava_voice_"))

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    async def _speak(self, text: str) -> None:
        """Synthesize ``text`` to a temp WAV and play it. Logs but never raises."""
        if not text.strip():
            return
        out = self._tmp / f"tts_{id(text)}.wav"
        try:
            await self._tts.synthesize(text, out)
            await play_wav(out)
        except AudioUnavailableError:
            logger.debug("audio unavailable for TTS — silent")
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
        if ev.kind != "log":
            return
        msg = (ev.data.get("msg") or ev.data.get("text") or "").strip()
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
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def voice_channel_from_env() -> VoiceChannel:
    """Build a VoiceChannel from environment variables.

    STT_PROVIDER / TTS_PROVIDER select backends (default: faster_whisper / piper).
    Raises RuntimeError if required env vars (e.g. PIPER_MODEL) are absent.
    """
    from yuyutsava.io.stt import stt_from_env
    from yuyutsava.io.tts import tts_from_env

    return VoiceChannel(tts=tts_from_env(), stt=stt_from_env())
