"""WebSocket transport for interactive agent conversations (text + voice).

``WS /ws/converse`` is the bidirectional channel the Electron/mobile UIs use to
talk to the daemon-hosted deepagent (see
:class:`yuyutsava.daemon.conversation_manager.ConversationManager`). It is the
non-CLI sibling of the terminal REPL: same :class:`ConversationService`, same
turn loop, different transport.

The socket is a **viewer, not the owner**. Turns are created by the daemon's
:class:`~yuyutsava.daemon.turn_registry.TurnRegistry` and addressed by
``thread_id``; this handler attaches to that thread on connect, streams its
frames, and merely *detaches* on disconnect. Closing a tinker pane, switching
TODO cards or reloading the renderer does not kill the agent — explicit
cancellation (``interrupt`` / ``POST /conversations/{thread_id}/cancel``) is
the only thing that does.

Protocol (JSON text frames)
---------------------------
Client → server:
  * ``{"type":"user_text","text":...}``   — run one turn (text)
  * ``{"type":"audio","pcm":<base64>}``   — a frame of 16 kHz mono int16 mic PCM
  * ``{"type":"audio_end"}``              — end of capture (push-to-talk release)
  * ``{"type":"ask_response","text":...}`` — answer a pending HITL interrupt
  * ``{"type":"interrupt"}``               — cancel the in-flight turn (barge-in)
  * ``{"type":"ping"}``

Connect query: ``origin`` / ``agent`` / ``card`` / ``resume_id`` / ``continue``
as before, plus ``since_seq=<n>`` — the last ``seq`` the client rendered. The
handshake then replays everything after it before resuming the live stream.
Omitting it means "I have no prior state": only the *in-flight* turn is
replayed, since finished turns are already in the client's session history.

Server → client:
  * ``{"type":"hello","session_id":...,"thread_id":...,"resuming":bool,
       "seq":int,"run":{...}|null}``      — ``run`` describes the in-flight turn
  * ``{"type":"turn_start","run_id":...,"text":...,"kind":"text"|"voice"}``
  * StreamEvent frames: ``token`` / ``tool_call`` / ``tool_result`` / ``image`` / ``log`` / ``final``
  * ``{"type":"speech_started"}``          — VAD detected the user talking
  * ``{"type":"transcript","text":...}``   — final STT of the user's utterance
  * ``{"type":"speaking_start"}`` / ``{"type":"speaking_end"}`` — TTS bracketing
  * ``{"type":"audio_chunk","pcm":<base64>,"sample_rate":int}`` — spoken reply PCM
  * ``{"type":"ask","ask":{...}}``         — interrupt; reply with ``ask_response``
  * ``{"type":"turn_end","status":...}``   — turn finished
  * ``{"type":"error","message":...}``

Every frame that belongs to the *turn* carries a monotonic per-thread ``seq``
and is replayable; connection-scoped frames (``hello``, ``pong``, mic state)
carry none and are sent straight down this socket.

The voice loop: mic ``audio`` frames are segmented by VAD; a completed utterance
is transcribed (``transcript``) and run as a turn whose prose is streamed back as
``audio_chunk`` frames. Talking while the agent talks does **not** cancel it by
default: mic input is ignored for the duration of a turn, and barge-in is opt-in
(``YUYUTSAVA_VOICE_BARGE_IN``) and then only fires on a transcribed utterance,
never a bare onset.

Dictation (``?mode=dictate``): the same socket in transcribe-only mode — no
session is opened and no agent turn ever runs. Mic ``audio`` frames are VAD-
segmented and each utterance comes back as a ``transcript`` frame; ``audio_end``
flushes the tail and the server closes the dictation with ``dictate_done``.
The client owns the text (the TODO note editor inserts it for the user to
edit — never auto-submitted). One dictation per connection.

HITL is handled inline on the socket (mirroring the CLI REPL's inline prompts)
rather than through the proposal/ask SSE — an interactive chat answers its own
questions in-band. Background async-subagent approvals still flow through the
daemon's existing channel router.

Auth: a ``?token=`` query param is consumed by the auth middleware; loopback
binds (the local Electron app) are exempt, matching ``/stream``.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request, WebSocket

from yuyutsava.config.env import env_flag, env_float
from yuyutsava.daemon.web.exceptions import ConflictError, ServiceUnavailableError
from yuyutsava.daemon.web.schemas.proposal import OkOut
from yuyutsava.daemon.web.services.converse_connection import (
    ConverseConnection,
    ConverseSpec,
    VoicePolicy,
)
from yuyutsava.daemon.web.services.converse_session import DictationSession
from yuyutsava.daemon.web.voice_pipeline import VoicePipeline
from yuyutsava.policy.interrupt_format import (
    body_for_interrupt,
    options_for_interrupt,
    title_for_interrupt,
)

logger = logging.getLogger("yuyutsava.daemon.web.routers.converse")


router = APIRouter(tags=["converse"])

# NOTE: conversation asks never expire — an ask is a durable record
# (daemon/ask_registry.py) that the user can answer from the owning chat, the
# Inbox or the always-on-top overlay, whenever they get to it. Silently
# rejecting on the user's behalf because they stepped away was never the right
# answer to "the agent is waiting".


# Grace window after the agent starts speaking during which VAD speech onsets are
# ignored for barge-in. Covers the initial TTS burst / echo settle so the agent's
# own voice can't self-interrupt; a real user talking over still cuts in after it.
# Tunable via YUYUTSAVA_BARGE_GRACE_SEC for noisy rooms (fan/AC settle time).
def _barge_grace_sec() -> float:
    return max(0.0, env_float("YUYUTSAVA_BARGE_GRACE_SEC", default=0.8))


_BARGE_GRACE_SEC = _barge_grace_sec()


# Minimum ASR confidence (0..1) below which a transcript is treated as garbled:
# the user is asked to repeat instead of running the agent on a bad transcript.
# Only backends that expose a confidence (faster-whisper) are gated; Groq and
# any backend returning None are never gated. Set 0 to disable the gate.
def _stt_min_confidence() -> float:
    return max(0.0, min(1.0, env_float("YUYUTSAVA_STT_MIN_CONFIDENCE", default=0.35)))


_STT_MIN_CONFIDENCE = _stt_min_confidence()


# Barge-in (interrupting the agent's spoken reply by talking over it). DISABLED by
# default: while the agent is speaking, mic input is ignored so its own TTS echo
# and room noise can't chop the reply off mid-sentence — the answer is always
# heard in full, and the user stops it with the UI Stop button. Set
# YUYUTSAVA_VOICE_BARGE_IN=1 to opt into voice interruption; even then it only ever
# triggers on a real, transcribed utterance (never a bare onset), and echo/noise
# that transcribes to nothing is ignored.
def _barge_in_enabled() -> bool:
    return env_flag("YUYUTSAVA_VOICE_BARGE_IN", true=("1", "true", "yes", "on"))


_BARGE_IN_ENABLED = _barge_in_enabled()


# How long after the agent stops speaking we keep ignoring mic input, so the tail
# of buffered playback (still audible, still hitting the mic) can't self-trigger.
def _post_speak_grace_sec() -> float:
    return max(0.0, env_float("YUYUTSAVA_VOICE_POST_SPEAK_GRACE_SEC", default=1.2))


_POST_SPEAK_GRACE_SEC = _post_speak_grace_sec()


async def _dictate(ws: WebSocket) -> None:
    """Hand the socket to a dictation session (``?mode=dictate``).

    The loop and its state live in
    :class:`~yuyutsava.daemon.web.services.converse_session.DictationSession`.
    The pipeline is built here and injected, so this module stays the only place
    that constructs a real :class:`VoicePipeline`.
    """
    await DictationSession(ws, VoicePipeline(), min_confidence=_STT_MIN_CONFIDENCE).run()


def _since_seq(ws: WebSocket) -> int | None:
    """``?since_seq=`` — the last per-thread ``seq`` this client rendered.

    ``None`` (absent/garbage) means "no prior state", which the registry reads
    as "replay only the in-flight turn" rather than the whole ring.
    """
    raw = (ws.query_params.get("since_seq") or "").strip()
    if not raw:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


@router.post(
    "/conversations/{thread_id}/cancel",
    response_model=OkOut,
    summary="Cancel the in-flight turn on a conversation thread",
)
async def cancel_conversation(thread_id: str, request: Request) -> OkOut:
    """Stop a running conversation turn — parity with ``POST /tasks/{id}/cancel``.

    Runs are daemon-owned and addressed by ``thread_id``, so any surface can
    stop one; a socket disconnect deliberately cannot.
    """
    manager = getattr(request.app.state, "conversation_manager", None)
    if manager is None:
        raise ServiceUnavailableError("conversation manager not initialized")
    if not manager.turns.cancel(thread_id):
        raise ConflictError(f"no turn is running on thread {thread_id!r}")
    return OkOut(ok=True, note="turn cancellation requested")


async def _refuse(ws: WebSocket, message: str) -> None:
    """Tell the client why, and close."""
    await ws.send_text(json.dumps({"type": "error", "message": message}))
    await ws.close()


def _ask_surface(agent: str, card_id: str | None, origin: str) -> tuple[str, str]:
    """Which surface *owns* an ask raised in this conversation, and its label.

    The only thing that decides where such an ask is allowed to render.
    """
    if agent == "tinker" and card_id:
        return "tinker", "TinkerAgent"
    if origin == "voice":
        return "voice", "Voice"
    return "chat", "Chat"


@router.websocket("/ws/converse")
async def converse(ws: WebSocket) -> None:
    manager = getattr(ws.app.state, "conversation_manager", None)
    await ws.accept()
    # Transcribe-only dictation shares the endpoint (and its auth handling) but
    # none of the conversation machinery — no manager, no session, no turns.
    if ws.query_params.get("mode") == "dictate":
        await _dictate(ws)
        return
    if manager is None:
        await _refuse(ws, "conversation manager not initialized")
        return

    q = ws.query_params
    origin = q.get("origin", "cli")
    resume_id = q.get("resume_id") or None
    # agent=tinker&card=<card_id> routes to the card-bound TinkerAgent bundle;
    # resume_id picks one of the card's chats (todo:<card_id>[:<suffix>]),
    # no resume_id starts a fresh one. Default is the shared master deepagent.
    agent = q.get("agent", "master")
    card_id = q.get("card") or None
    try:
        convo, resuming = await manager.open(
            agent=agent,
            card_id=card_id,
            origin=origin,
            resume_id=resume_id,
            continue_latest=q.get("continue", "").lower() in ("1", "true", "yes"),
        )
    except Exception as exc:  # noqa: BLE001 — any failure is sent as a refusal
        logger.warning("converse: failed to open conversation", exc_info=True)
        await _refuse(ws, str(exc))
        return

    logger.info(
        "converse: WS open agent=%s card=%s origin=%s resume_id=%s → session=%s "
        "resuming=%s voice_barge_in=%s (post_speak_grace=%.1fs)",
        agent,
        card_id,
        origin,
        resume_id,
        convo.session_id,
        resuming,
        _BARGE_IN_ENABLED,
        _POST_SPEAK_GRACE_SEC,
    )
    surface, agent_label = _ask_surface(agent, card_id, origin)
    await ConverseConnection(
        ws,
        ConverseSpec(
            convo=convo,
            manager=manager,
            origin=origin,
            agent=agent,
            card_id=card_id,
            surface=surface,
            agent_label=agent_label,
            resuming=resuming,
            # HITL goes through the daemon's shared channel router, so a
            # conversation ask is the same durable record as a background one:
            # it gets an ask_id, is persisted before broadcast, and can be
            # answered from the Inbox or the overlay as well as inline here.
            # ``decisions`` resolves an inline answer through that one path.
            channels=getattr(ws.app.state, "channels", None),
            decisions=getattr(ws.app.state, "decision_service", None),
            # A replayable spoken-reply surface; None in zero-config setups.
            voice_store=getattr(manager, "voice_store", None),
            voice_toggles=getattr(ws.app.state, "runtime_settings", None),
        ),
        since_seq=_since_seq(ws),
        # A lambda, not the class: both WS test files patch ``VoicePipeline`` on
        # THIS module, and resolving the name inside the connection would
        # silently detach that — the failure mode is not a red test but a test
        # run loading real STT models.
        voice_factory=lambda: VoicePipeline(),
        policy=VoicePolicy(
            barge_in_enabled=_BARGE_IN_ENABLED,
            post_speak_grace=_POST_SPEAK_GRACE_SEC,
            min_confidence=_STT_MIN_CONFIDENCE,
        ),
        describe_interrupt=lambda iv: (
            title_for_interrupt(iv),
            body_for_interrupt(iv),
            options_for_interrupt(iv),
        ),
    ).run()
