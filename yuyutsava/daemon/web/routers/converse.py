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
TODO cards or reloading the renderer no longer kills the agent — explicit
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
  * ``{"type":"ask","payload":{...}}``     — interrupt; reply with ``ask_response``
  * ``{"type":"turn_end","status":...}``   — turn finished
  * ``{"type":"error","message":...}``

Every frame that belongs to the *turn* carries a monotonic per-thread ``seq``
and is replayable; connection-scoped frames (``hello``, ``pong``, mic state)
carry none and are sent straight down this socket.

The voice loop: mic ``audio`` frames are segmented by VAD; a completed utterance
is transcribed (``transcript``) and run as a turn whose prose is streamed back as
``audio_chunk`` frames. Speaking while the agent talks (``speech_started`` during
a turn) cancels it — barge-in.

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

import asyncio
import base64
import contextlib
import json
import logging
import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from yuyutsava.config.env import env_flag, env_float
from yuyutsava.context.meter import bus
from yuyutsava.core.streaming import StreamEvent
from yuyutsava.daemon.turn_registry import TurnRun
from yuyutsava.daemon.web.exceptions import ConflictError, ServiceUnavailableError
from yuyutsava.daemon.web.schemas.proposal import OkOut
from yuyutsava.daemon.web.services.converse_session import (
    AskOwnership,
    DictationSession,
    InlineAsks,
    MicDeps,
    SpeakingState,
    SpokenReply,
    TurnPlumbing,
    UtteranceRouter,
)
from yuyutsava.daemon.web.voice_pipeline import VoicePipeline
from yuyutsava.policy.interrupt_format import (
    body_for_interrupt,
    options_for_interrupt,
    title_for_interrupt,
)
from yuyutsava.ports.channels import DecisionConflictError
from yuyutsava.storage.voice_blobs import write_voice_wav

logger = logging.getLogger("yuyutsava.daemon.web.routers.converse")


def _latest_usage_snapshot(thread_id: str) -> dict | None:
    """Last context-meter reading for a thread, or None. Never raises.

    Telemetry must not be able to fail a handshake, so a bad read degrades to
    "no numbers yet" — which is also what a fresh conversation looks like.
    """
    try:
        snap = bus().latest(thread_id)
        return snap.as_dict() if snap is not None else None
    except Exception:  # noqa: BLE001
        logger.debug("usage snapshot unavailable for hello", exc_info=True)
        return None


router = APIRouter(tags=["converse"])

# NOTE: conversation asks used to auto-reject after 300 s. They no longer
# expire at all — an ask is a durable record (daemon/ask_registry.py) that the
# user can answer from the owning chat, the Inbox or the always-on-top overlay,
# whenever they get to it. Silently rejecting on the user's behalf because they
# stepped away was never the right answer to "the agent is waiting".

# Bytes per outbound audio frame (~0.5s of 16 kHz mono int16) — small enough to
# start playback quickly, large enough to avoid WS frame overhead.


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


async def _tts_enabled(ws: WebSocket) -> bool:
    """Whether spoken replies are on right now (voice-mode runtime toggle).

    Read per turn, not per connection: a user who turns voice mode off mid-chat
    should stop being spoken to on the *next* answer without reconnecting. A
    reply already being synthesized finishes — cutting a live sentence in half
    is worse than one extra spoken answer. The refresh is TTL-guarded (usually
    a no-op) and catches a toggle written by another process, e.g. ``/voice
    off`` typed into a CLI REPL. Defaults to on when the daemon has no settings
    wired (tests / embedded use).
    """
    settings = getattr(ws.app.state, "runtime_settings", None)
    if settings is None:
        return True
    await settings.refresh()
    return settings.voice().tts_enabled


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
        await ws.send_text(
            json.dumps({"type": "error", "message": "conversation manager not initialized"})
        )
        await ws.close()
        return

    # Voice turns persist a replayable conversation surface (Phase 6b). None in
    # zero-config setups where the manager wasn't given a store.
    voice_store = getattr(manager, "voice_store", None)

    origin = ws.query_params.get("origin", "cli")
    resume_id = ws.query_params.get("resume_id") or None
    continue_latest = ws.query_params.get("continue", "").lower() in ("1", "true", "yes")
    # agent=tinker&card=<card_id> routes to the card-bound TinkerAgent bundle;
    # resume_id picks one of the card's chats (todo:<card_id>[:<suffix>]),
    # no resume_id starts a fresh one. Default is the shared master deepagent.
    agent = ws.query_params.get("agent", "master")
    card_id = ws.query_params.get("card") or None

    try:
        convo, resuming = await manager.open(
            agent=agent,
            card_id=card_id,
            origin=origin,
            resume_id=resume_id,
            continue_latest=continue_latest,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("converse: failed to open conversation", exc_info=True)
        await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await ws.close()
        return

    # Turns live in the daemon, keyed by thread. This connection is one viewer
    # of that thread among possibly several (a reopened pane, the overlay, a
    # second window) — none of them owns the run.
    registry = manager.turns
    thread_id = convo.thread_id
    # HITL goes through the daemon's shared channel router, so a conversation
    # ask is the same durable record as a background one: it gets an ask_id, is
    # persisted before broadcast, and can be answered from the Inbox or the
    # overlay as well as inline here. ``decisions`` resolves an inline answer
    # through that one code path instead of a socket-local future.
    channels = getattr(ws.app.state, "channels", None)
    decisions = getattr(ws.app.state, "decision_service", None)
    # Which surface *owns* an ask raised in this conversation — the only thing
    # that decides where it is allowed to render.
    if agent == "tinker" and card_id:
        surface, agent_label = "tinker", "TinkerAgent"
    elif origin == "voice":
        surface, agent_label = "voice", "Voice"
    else:
        surface, agent_label = "chat", "Chat"

    _voice_toggles = getattr(ws.app.state, "runtime_settings", None)
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

    send_lock = asyncio.Lock()

    async def _send(obj: dict) -> None:
        """Connection-scoped write: ``hello``, ``pong``, mic state, replay.

        Frames that belong to the *turn* never come through here directly —
        they go onto the run's channel and reach every viewer via the pump.
        """
        async with send_lock:
            await ws.send_text(json.dumps(obj, default=str))

    # Attach as a viewer BEFORE the handshake goes out, so a frame emitted
    # between "read the ring" and "start the pump" can't fall through the gap.
    _chan, replay, sub_q, floor = registry.attach(thread_id, _since_seq(ws))
    live_run: TurnRun | None = _chan.run
    last_sent_seq = replay[-1]["seq"] if replay else floor

    await _send(
        {
            "type": "hello",
            "session_id": convo.session_id,
            "thread_id": convo.thread_id,
            "origin": origin,
            "agent": agent,
            "card_id": card_id,
            "resuming": resuming,
            # Where this thread's frame counter stands, and what (if anything) is
            # running on it right now — so a client that reattaches mid-turn knows
            # to stay in its "busy" state instead of releasing the composer.
            "seq": _chan.seq,
            "run": live_run.to_wire() if live_run is not None else None,
            # Tell the client the voice interruption policy so it can match: when
            # barge-in is off (default) the client runs half-duplex — it mutes the
            # mic while the reply is still playing so noise can't cut it off.
            "barge_in": _BARGE_IN_ENABLED,
            # Voice-mode snapshot at connect. The live source of truth is the
            # `settings` SSE item (which every surface, including the overlay,
            # already listens to); this is just so a freshly-opened panel renders
            # the right affordance before its first SSE frame arrives.
            "voice": (
                _voice_toggles.voice().to_dict()
                if _voice_toggles is not None
                else {"wake_enabled": True, "tts_enabled": True}
            ),
            # Last known context/spend reading for this conversation, so a panel
            # opened between turns shows real numbers instead of waiting for the
            # next model call to say anything.
            "usage": _latest_usage_snapshot(convo.thread_id),
        }
    )
    # The gap this client missed while it was away (or the in-flight turn from
    # its start, for a client with no prior state), oldest first.
    for frame in replay:
        await _send(frame)

    async def _pump() -> None:
        """Relay this thread's live frames onto the socket, in seq order."""
        nonlocal last_sent_seq
        try:
            while True:
                frame = await sub_q.get()
                seq = frame.get("seq", 0)
                if seq <= last_sent_seq:
                    continue  # already replayed at handshake
                last_sent_seq = seq
                await _send(frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a dead socket just ends the pump
            logger.debug("converse: pump ended for %s", thread_id, exc_info=True)

    pump_task = asyncio.create_task(_pump(), name=f"converse-pump:{thread_id}")

    # Shared per-connection state, mutated by the single receive loop below.
    voice: VoicePipeline | None = None  # built lazily on first audio frame
    # Runs THIS connection started that speak through ITS VoicePipeline. The
    # pipeline is genuinely per-connection (it owns mic frames) but closing it
    # out from under a reply still being synthesized would cut the answer off,
    # so on disconnect we hand it to the run to close instead.
    voice_runs: set[str] = set()
    # Background best-effort tasks (model pre-warm, utterance transcription)
    # kept referenced so they aren't GC'd mid-flight; cancelled on disconnect.
    bg_tasks: set[asyncio.Task] = set()

    def _spawn_bg(coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        bg_tasks.add(t)
        t.add_done_callback(bg_tasks.discard)
        return t

    # Pre-warm the (heavy) agent bundle in the background so the first turn isn't a
    # cold start — overlaps the one-time build with the user speaking. No-op once
    # the shared bundle exists.
    if not convo.bundle_ready:
        _spawn_bg(convo.prewarm())
    # Mic guards, read by the audio branch and written by whichever turn body is
    # running. ``speaking`` is True while the agent's TTS is playing (with the
    # monotonic time it started) so self-echo can be ignored during the grace
    # window; ``speaking_ended_at`` marks when the last reply finished, because
    # the client is still draining buffered audio and the mic still hears it.
    # ``agent_active`` is True while an ACTUAL turn is executing, as opposed to
    # merely transcribing an utterance that may turn out to be empty noise — the
    # audio branch protects an active turn from trailing words, but still lets a
    # fresh utterance replace one that is only transcribing.
    #
    # One object rather than four ``nonlocal`` names: they are connection-scoped
    # (the run they describe may outlive this socket) and the turn body that
    # writes them now lives outside this function.
    speaking = SpeakingState()

    async def _reject_busy() -> None:
        """Refuse a second turn — to THIS socket only.

        The thread is already streaming a turn to every viewer; broadcasting a
        "busy" error onto the channel would blank it out for all of them.
        """
        await _send(
            {
                "type": "error",
                "message": "a turn is already running on this conversation",
            }
        )
        await _send({"type": "turn_end"})

    @contextlib.asynccontextmanager
    async def _meter_frames(run: TurnRun):
        """Forward context-meter snapshots to this conversation's viewers.

        Subscribed per turn rather than per socket so the frames stop when the
        turn does, and scoped to this thread so a second conversation's window
        never lands on this one's panel.

        Not a ``StreamEvent`` kind on purpose: the meter publishes from inside
        the model step, and routing it through the graph stream would mean
        teaching core/streaming, both CLI renderers, the SSE payload union and
        the static web UI about a frame only this handler can use.
        """

        def _forward(snap) -> None:
            # emit() is a put_nowait fan-out; safe to call from the model step.
            run.emit({"type": "usage", **snap.as_dict()})

        token = bus().subscribe(convo.thread_id, _forward)
        try:
            yield
        finally:
            bus().unsubscribe(token)

    def _event_sink(run: TurnRun):
        async def _sink(ev: StreamEvent) -> None:
            # Link any background task this turn launches back to this
            # conversation thread, so its completion wakes the master here
            # (subagent_completed).
            manager.record_async_launch(ev, thread_id=thread_id, origin=origin)
            run.emit({"type": ev.kind, **ev.data})

        return _sink

    _asks = InlineAsks(
        AskOwnership(
            session_id=convo.session_id,
            thread_id=thread_id,
            origin=origin,
            surface=surface,
            card_id=card_id,
            agent_label=agent_label,
        ),
        channels=channels,
        describe=lambda iv: (
            title_for_interrupt(iv),
            body_for_interrupt(iv),
            options_for_interrupt(iv),
        ),
    )

    async def _run_turn(run: TurnRun) -> None:
        """Text turn body. Errors, cancellation and ``turn_end`` are the
        registry's job (``TurnRegistry._drive``) — it terminates a run exactly
        once however it ends, for every viewer at the same time."""
        speaking.agent_active = True
        try:
            if not convo.bundle_ready:
                run.emit({"type": "log", "text": "preparing agent (first run)…"})
            async with _meter_frames(run):
                await convo.run_turn(
                    run.text,
                    on_event=_event_sink(run),
                    ask_handler=_asks.handler(run),
                    run_name=f"{origin}-chat",
                    keep_full_payloads=True,
                )
        finally:
            speaking.agent_active = False

    # ---- voice path ------------------------------------------------------

    def _ensure_voice() -> VoicePipeline:
        nonlocal voice
        if voice is None:
            voice = VoicePipeline()
            # Warm STT/TTS off the loop so the first spoken turn isn't a cold
            # model load. Overlaps with the user's first utterance.
            _spawn_bg(voice.prewarm())
        return voice

    async def _persist_voice_message(
        *,
        role: str,
        modality: str,
        text: str,
        pcm: bytes | None = None,
        rate: int | None = None,
    ) -> None:
        """Best-effort append of one turn to the replayable voice history.

        Writes the TTS audio (if any) to disk off the loop, then the DB row.
        Persistence never breaks a live turn — failures are logged and dropped.
        """
        if voice_store is None:
            return
        try:
            blob_path = None
            if pcm and rate:
                blob_path = await asyncio.to_thread(
                    write_voice_wav,
                    convo.thread_id,
                    pcm,
                    rate,
                )
            await voice_store.put_message(
                convo.thread_id,
                role=role,
                modality=modality,
                text=text or "",
                audio_blob_path=blob_path,
                sample_rate=rate if blob_path else None,
            )
        except Exception:  # noqa: BLE001
            logger.warning("converse: persisting voice message failed", exc_info=True)

    async def _run_voice_turn(run: TurnRun) -> None:
        """Body for a spoken turn — the streaming lives in :class:`SpokenReply`."""
        assert voice is not None
        await SpokenReply(
            run,
            voice=voice,
            state=speaking,
            plumbing=TurnPlumbing(
                convo=convo,
                origin=origin,
                event_sink=_event_sink,
                ask_handler=_asks.handler,
                meter_frames=_meter_frames,
            ),
            persist=_persist_voice_message if voice_store is not None else None,
        ).run()

    def _start_turn(text: str, *, spoken: bool) -> bool:
        """Hand one turn to the daemon. False when the thread is already busy."""
        run = manager.start_turn(
            thread_id,
            body=_run_voice_turn if spoken else _run_turn,
            session_id=convo.session_id,
            origin=origin,
            agent=agent,
            card_id=card_id,
            kind="voice" if spoken else "text",
            text=text,
            on_done=_utterances.on_turn_done,
        )
        if run is None:
            return False
        if spoken:
            voice_runs.add(run.run_id)
        return True

    async def _start_spoken_turn(text: str, *, preempt: bool = False) -> None:
        """Answer a spoken utterance, aloud or silently per the voice toggle.

        With TTS off the turn still runs exactly as a mic-driven turn — the
        transcript is echoed, the answer streams — it just isn't synthesized.
        That is the "manual mic still works, nothing talks back" mode.
        ``preempt`` waits out the turn it is replacing (barge-in / a new
        push-to-talk press) so the thread's turn gate doesn't reject the new one.
        """
        if preempt:
            await registry.cancel_and_wait(thread_id)
        spoken = voice is not None and await _tts_enabled(ws)
        if not _start_turn(text, spoken=spoken):
            await _reject_busy()

    # What a captured utterance becomes — a turn, a queued follow-up, or
    # nothing — including the talk-over guess. Built here because it needs this
    # socket's pipeline and its send/persist/turn calls.
    _utterances = UtteranceRouter(
        MicDeps(
            voice_of=lambda: voice,
            send=_send,
            start_turn=_start_spoken_turn,
            persist=_persist_voice_message,
            spawn=_spawn_bg,
        ),
        origin=origin,
        min_confidence=_STT_MIN_CONFIDENCE,
        barge_in_enabled=_BARGE_IN_ENABLED,
    )

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                await _send({"type": "error", "message": "invalid JSON frame"})
                continue
            mtype = msg.get("type")

            if mtype == "ping":
                await _send({"type": "pong"})
                continue

            if mtype == "ask_response":
                text = str(msg.get("text", ""))
                run = registry.active(thread_id)
                ask_id = msg.get("ask_id") or (run.pending_ask_id if run else None)
                # Answer whichever mechanism is actually waiting. ``_ask`` picks
                # its wait on ``channels``; deciding here on ``decisions`` alone
                # would disagree with it, because ``create_app`` always builds a
                # DecisionService but ``channels`` may be absent (tests,
                # embedded). In that pairing the turn parks on a future nobody
                # resolves — no ``final``, no ``turn_end``, forever.
                # ``pending_ask`` is set only by that run-local branch, so a
                # future that accepts the answer identifies it exactly.
                if run is not None and run.answer_ask(text):
                    continue
                # The hub path: an answer given here is indistinguishable from
                # one given in the Inbox or the overlay — same record, same
                # resolution, same ask_resolved broadcast clearing every surface.
                if ask_id and decisions is not None:
                    # An ask is visible here before the hub registers the future
                    # that carries its answer, so answering immediately can beat
                    # the waiter. This run raised this ask and is still blocked
                    # on it, so the answer is worth holding rather than
                    # rejecting (`R-BUG-09`).
                    in_flight = run is not None and run.pending_ask_id == ask_id
                    try:
                        await decisions.respond_ask(str(ask_id), text, hold_if_unwaited=in_flight)
                    except DecisionConflictError:
                        # Someone answered it elsewhere first — that's the
                        # design, not an error. Tell this socket so it clears.
                        await _send({"type": "ask_resolved", "ask_id": ask_id})
                continue

            if mtype == "interrupt":
                registry.cancel(thread_id)
                continue

            if mtype == "user_text":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                # Board-UI selection context (objective/note reference ids)
                # rides a structured field and is composed here — the sentinel
                # wrapper is canonical server-side, and clients strip it from
                # hydrated transcripts so the user only ever sees their text.
                context = (msg.get("context") or "").strip()
                if context:
                    text = f"<selection-context>\n{context}\n</selection-context>\n\n{text}"
                if not _start_turn(text, spoken=False):
                    await _reject_busy()
                continue

            if mtype == "audio":
                b64 = msg.get("pcm")
                if not b64:
                    continue
                try:
                    pcm = base64.b64decode(b64)
                except (ValueError, TypeError):
                    continue
                vp = _ensure_voice()
                # Unless voice barge-in is explicitly enabled, ignore mic input for
                # the WHOLE duration of an active turn (thinking + speaking + a
                # short tail while buffered audio drains) so the agent's own TTS
                # echo / room noise can't cut the reply off or queue a spurious
                # follow-up turn. The answer is always heard in full; the user
                # interrupts with the UI Stop button instead. (The client also
                # mutes its mic while the reply is still PLAYING — see hello
                # barge_in — which is the authoritative guard, since only the
                # client knows when playback truly ends.)
                turn_running = registry.active(thread_id) is not None
                if not _BARGE_IN_ENABLED:
                    turn_active = (
                        turn_running
                        or _utterances.pending()
                        or speaking.agent_active
                        or speaking.speaking
                        or (time.monotonic() - speaking.speaking_ended_at) < _POST_SPEAK_GRACE_SEC
                    )
                    if turn_active:
                        continue
                res = vp.feed_audio(pcm)
                if res.speech_started:
                    # A raw speech ONSET never interrupts the agent. While it is
                    # SPEAKING, an onset is almost always room noise or the agent's
                    # own TTS echo — cutting the reply here is exactly the "it stops
                    # mid-sentence" bug. A genuine talk-over is handled below, once a
                    # full utterance is captured AND transcribes to real words. While
                    # the agent is THINKING or idle we only surface the listening
                    # indicator (also non-cancelling) so the user sees they're heard.
                    if not speaking.speaking:
                        await _send({"type": "speech_started"})
                if res.utterance is not None:
                    if speaking.speaking:
                        # Possible talk-over. Don't cancel yet — verify it's real
                        # speech (transcribe-before-cancel) so noise/echo can't chop
                        # the reply off. Runs off to the side while the agent keeps
                        # speaking; only confirmed words actually interrupt.
                        _spawn_bg(_utterances.maybe_barge_in(res.utterance))
                    elif speaking.agent_active or turn_running:
                        # The agent is mid-answer (thinking) — don't interrupt it,
                        # but don't drop what the user just said either. Queue this
                        # completed utterance (latest wins) so it runs as the next
                        # turn the moment the current one ends. Keeps the mic
                        # continuously useful without killing the in-flight answer.
                        _utterances.queue(res.utterance)
                        logger.info(
                            "voice[%s]: utterance queued — runs after current turn",
                            origin,
                        )
                    else:
                        # Idle, or only transcribing a prior (possibly-empty noise)
                        # utterance — let this newer utterance win: the spawn
                        # cancels the stale transcription and runs this one.
                        _utterances.hear(res.utterance)
                continue

            if mtype == "audio_end":
                # Explicit end of capture (push-to-talk release): flush the VAD.
                if voice is not None:
                    utt = voice.flush()
                    if utt:
                        _utterances.hear(utt, preempt=True)
                continue

            await _send({"type": "error", "message": f"unknown message type {mtype!r}"})

    except WebSocketDisconnect:
        pass
    finally:
        # This socket was only ever a VIEWER. Detach — never cancel: the run
        # keeps going on the daemon loop and whichever client comes back next
        # replays the gap with ?since_seq=.
        registry.detach(thread_id, sub_q)
        pump_task.cancel()
        for t in list(bg_tasks):
            if not t.done():
                t.cancel()
        if voice is not None:
            live = registry.active(thread_id)
            if live is not None and live.run_id in voice_runs and live.task is not None:
                # This connection's pipeline is still synthesizing that reply —
                # closing it now would cut the answer off mid-sentence. Hand it
                # to the run to close when it ends.
                live.task.add_done_callback(lambda _t, v=voice: v.close())
            else:
                voice.close()
        # A session opened but never used (tab/overlay connect with nothing said,
        # or a flaky reconnect) is just clutter — drop it so the Sessions history
        # doesn't fill with empty seconds-old rows. Only freshly-created sessions
        # qualify; a resumed one is left alone. Otherwise mark it idle (not done)
        # so a reconnect (--resume) can continue it. Skipped entirely while a turn
        # is still running on this thread: it is anything but idle, and discarding
        # the row would orphan a live run. Best-effort.
        try:
            if registry.active(thread_id) is None:
                discarded = (not resuming) and await convo.discard_if_unused()
                if not discarded:
                    await convo.finish("idle")
        except Exception:  # noqa: BLE001
            pass
