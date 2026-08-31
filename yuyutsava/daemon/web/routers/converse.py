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
import json
import logging
import os
import time
import uuid

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from yuyutsava.audio_io.blobs import write_voice_wav
from yuyutsava.audio_io.sentence import SentenceChunker
from yuyutsava.core.streaming import StreamEvent
from yuyutsava.daemon.channels import AskPrompt
from yuyutsava.daemon.interrupt_format import (
    body_for_interrupt, options_for_interrupt, title_for_interrupt,
)
from yuyutsava.daemon.turn_registry import TurnRun
from yuyutsava.daemon.web.exceptions import ConflictError, ServiceUnavailableError
from yuyutsava.daemon.web.schemas.proposal import OkOut
from yuyutsava.daemon.web.voice_pipeline import VoicePipeline

logger = logging.getLogger("yuyutsava.daemon.web.routers.converse")

router = APIRouter(tags=["converse"])

# NOTE: conversation asks used to auto-reject after 300 s. They no longer
# expire at all — an ask is a durable record (daemon/ask_registry.py) that the
# user can answer from the owning chat, the Inbox or the always-on-top overlay,
# whenever they get to it. Silently rejecting on the user's behalf because they
# stepped away was never the right answer to "the agent is waiting".

# Bytes per outbound audio frame (~0.5s of 16 kHz mono int16) — small enough to
# start playback quickly, large enough to avoid WS frame overhead.
_AUDIO_FRAME_BYTES = 16_000

# Grace window after the agent starts speaking during which VAD speech onsets are
# ignored for barge-in. Covers the initial TTS burst / echo settle so the agent's
# own voice can't self-interrupt; a real user talking over still cuts in after it.
# Tunable via YUYUTSAVA_BARGE_GRACE_SEC for noisy rooms (fan/AC settle time).
def _barge_grace_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("YUYUTSAVA_BARGE_GRACE_SEC", "0.8")))
    except ValueError:
        return 0.8


_BARGE_GRACE_SEC = _barge_grace_sec()


# Minimum ASR confidence (0..1) below which a transcript is treated as garbled:
# the user is asked to repeat instead of running the agent on a bad transcript.
# Only backends that expose a confidence (faster-whisper) are gated; Groq and
# any backend returning None are never gated. Set 0 to disable the gate.
def _stt_min_confidence() -> float:
    try:
        return max(0.0, min(1.0, float(os.environ.get("YUYUTSAVA_STT_MIN_CONFIDENCE", "0.35"))))
    except ValueError:
        return 0.35


_STT_MIN_CONFIDENCE = _stt_min_confidence()


# Barge-in (interrupting the agent's spoken reply by talking over it). DISABLED by
# default: while the agent is speaking, mic input is ignored so its own TTS echo
# and room noise can't chop the reply off mid-sentence — the answer is always
# heard in full, and the user stops it with the UI Stop button. Set
# YUYUTSAVA_VOICE_BARGE_IN=1 to opt into voice interruption; even then it only ever
# triggers on a real, transcribed utterance (never a bare onset), and echo/noise
# that transcribes to nothing is ignored.
def _barge_in_enabled() -> bool:
    return os.environ.get("YUYUTSAVA_VOICE_BARGE_IN", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


_BARGE_IN_ENABLED = _barge_in_enabled()

# How long after the agent stops speaking we keep ignoring mic input, so the tail
# of buffered playback (still audible, still hitting the mic) can't self-trigger.
def _post_speak_grace_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("YUYUTSAVA_VOICE_POST_SPEAK_GRACE_SEC", "1.2")))
    except ValueError:
        return 1.2


_POST_SPEAK_GRACE_SEC = _post_speak_grace_sec()


async def _dictate(ws: WebSocket) -> None:
    """Transcribe-only loop for ``?mode=dictate`` (STT dictation, no agent).

    Reuses the exact voice plumbing of a conversation — :class:`VoicePipeline`
    VAD → STT — but never opens a session and never runs a turn. Utterances are
    transcribed strictly in capture order by a single worker (transcription can
    take seconds; the receive loop must keep feeding the VAD meanwhile), each
    non-empty transcript is sent as its own ``transcript`` frame, and the
    ``audio_end`` flush is acknowledged with ``dictate_done`` once the tail has
    drained. Low-confidence transcripts are dropped like the conversation path
    drops them — hallucinated noise is worse than a gap in an editable textarea.
    """
    voice = VoicePipeline()
    send_lock = asyncio.Lock()
    utterances: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def _send(obj: dict) -> None:
        async with send_lock:
            await ws.send_text(json.dumps(obj, default=str))

    async def _worker() -> None:
        while True:
            utt = await utterances.get()
            if utt is None:
                await _send({"type": "dictate_done"})
                return
            result = await voice.transcribe_detailed(utt)
            text = result.text
            conf = result.confidence
            if text and conf is not None and _STT_MIN_CONFIDENCE > 0 and conf < _STT_MIN_CONFIDENCE:
                logger.info(
                    "dictate: low-confidence transcript dropped (%.2f) %r",
                    conf, text[:80],
                )
                continue
            if text:
                logger.info("dictate: transcript=%r", text[:120])
                await _send({"type": "transcript", "text": text})

    prewarm = asyncio.create_task(voice.prewarm())
    worker = asyncio.create_task(_worker())
    await _send({"type": "hello", "mode": "dictate"})
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

            if mtype == "audio":
                b64 = msg.get("pcm")
                if not b64:
                    continue
                try:
                    pcm = base64.b64decode(b64)
                except (ValueError, TypeError):
                    continue
                res = voice.feed_audio(pcm)
                if res.speech_started:
                    await _send({"type": "speech_started"})
                if res.utterance is not None:
                    utterances.put_nowait(res.utterance)
                continue

            if mtype == "audio_end":
                # End of capture: flush the VAD tail, then let the worker drain
                # in order and ack with dictate_done behind the last transcript.
                tail = voice.flush()
                if tail:
                    utterances.put_nowait(tail)
                utterances.put_nowait(None)
                continue

            await _send({"type": "error", "message": f"unknown message type {mtype!r}"})

    except WebSocketDisconnect:
        pass
    finally:
        prewarm.cancel()
        worker.cancel()
        try:
            await worker
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        voice.close()


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
        await ws.send_text(json.dumps(
            {"type": "error", "message": "conversation manager not initialized"}
        ))
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
            agent=agent, card_id=card_id,
            origin=origin, resume_id=resume_id, continue_latest=continue_latest,
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
        agent, card_id, origin, resume_id, convo.session_id, resuming,
        _BARGE_IN_ENABLED, _POST_SPEAK_GRACE_SEC,
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

    await _send({
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
            _voice_toggles.voice().to_dict() if _voice_toggles is not None
            else {"wake_enabled": True, "tts_enabled": True}
        ),
    })
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

    def _spawn_bg(coro) -> None:
        t = asyncio.create_task(coro)
        bg_tasks.add(t)
        t.add_done_callback(bg_tasks.discard)

    # Pre-warm the (heavy) agent bundle in the background so the first turn isn't a
    # cold start — overlaps the one-time build with the user speaking. No-op once
    # the shared bundle exists.
    if not convo.bundle_ready:
        _spawn_bg(convo.prewarm())
    # Barge-in guard: True while the agent's TTS is playing, with the monotonic
    # time it started, so the audio handler can ignore self-echo during the grace
    # window (set in the TTS worker below). ``speaking_ended_at`` marks when the
    # last reply finished so we keep ignoring mic input for a short grace after —
    # the client is still draining buffered audio and the mic still hears it.
    # These are mic guards, so they stay connection-scoped even though the run
    # they describe may outlive this socket.
    agent_speaking = False
    speaking_since = 0.0
    speaking_ended_at = 0.0
    # True while an ACTUAL agent turn is executing (a non-empty transcript has
    # started run_turn), as opposed to merely transcribing an utterance that may
    # turn out to be empty noise. The audio handler protects an active turn from
    # being cancelled by trailing words / noise, but still lets a fresh utterance
    # replace a turn that's only transcribing.
    agent_active = False

    async def _reject_busy() -> None:
        """Refuse a second turn — to THIS socket only.

        The thread is already streaming a turn to every viewer; broadcasting a
        "busy" error onto the channel would blank it out for all of them.
        """
        await _send({
            "type": "error",
            "message": "a turn is already running on this conversation",
        })
        await _send({"type": "turn_end"})

    def _event_sink(run: TurnRun):
        async def _sink(ev: StreamEvent) -> None:
            # Link any background task this turn launches back to this
            # conversation thread, so its completion wakes the master here
            # (subagent_completed).
            manager.record_async_launch(ev, thread_id=thread_id, origin=origin)
            run.emit({"type": ev.kind, **ev.data})
        return _sink

    def _ask_handler(run: TurnRun):
        async def _ask(interrupt_value) -> str:
            iv = (
                dict(interrupt_value) if isinstance(interrupt_value, dict)
                else {"type": "user_question", "question": str(interrupt_value)}
            )
            ask = AskPrompt(
                ask_id=str(uuid.uuid4()),
                title=title_for_interrupt(iv),
                body=body_for_interrupt(iv),
                options=options_for_interrupt(iv),
                interrupt_value=iv,
                session_id=convo.session_id,
                agent_path=iv.get("agent_path") or origin,
                # Ownership. This ask belongs to THIS conversation and may only
                # render inline here; everywhere else it is a notification plus
                # an Inbox entry. A permission prompt must never appear inside
                # somebody's other running session.
                surface=surface,
                thread_id=thread_id,
                card_id=card_id,
                agent_label=agent_label,
                interrupt_id=iv.get("interrupt_id"),
            )
            run.pending_ask_id = ask.ask_id
            # Inline frame for whoever is viewing this thread — the same wire
            # record the SSE/inbox/overlay surfaces receive, so all of them
            # render from one shape through one shared card.
            run.emit({"type": "ask", "ask": ask.to_wire_dict()})
            try:
                if channels is not None:
                    # Joins the hub: persisted before broadcast, visible on
                    # every surface, answerable via POST /ask/{id}/respond from
                    # any of them — first answer anywhere wins.
                    return await channels.post_ask(ask)
                # No channel router (tests / embedded use): fall back to the
                # run-local future so the inline prompt still works.
                loop = asyncio.get_running_loop()
                fut: asyncio.Future[str] = loop.create_future()
                run.pending_ask = fut
                return await fut
            finally:
                run.pending_ask = None
                run.pending_ask_id = None
                # However it was answered — inline here, from the Inbox, from
                # the overlay, or by the turn being cancelled — every viewer of
                # this thread clears its inline card. Without this, answering in
                # the Inbox would leave a dead prompt sitting in the chat.
                run.emit({"type": "ask_resolved", "ask_id": ask.ask_id})
        return _ask

    async def _run_turn(run: TurnRun) -> None:
        """Text turn body. Errors, cancellation and ``turn_end`` are the
        registry's job (``TurnRegistry._drive``) — it terminates a run exactly
        once however it ends, for every viewer at the same time."""
        nonlocal agent_active
        agent_active = True
        try:
            if not convo.bundle_ready:
                run.emit({"type": "log", "text": "preparing agent (first run)…"})
            await convo.run_turn(
                run.text,
                on_event=_event_sink(run),
                ask_handler=_ask_handler(run),
                run_name=f"{origin}-chat",
                keep_full_payloads=True,
            )
        finally:
            agent_active = False

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
        *, role: str, modality: str, text: str,
        pcm: bytes | None = None, rate: int | None = None,
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
                    write_voice_wav, convo.thread_id, pcm, rate,
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
        """Run a turn whose prose is also spoken back as streamed audio.

        Agent tokens are accumulated into sentences and synthesized one at a
        time on a side queue, so audio starts on sentence 1 while the model is
        still writing. Cancellation (barge-in / interrupt) stops both the turn
        and any queued/in-flight TTS immediately.
        """
        nonlocal agent_active
        agent_active = True
        assert voice is not None
        chunker = SentenceChunker()
        tts_queue: asyncio.Queue[str | None] = asyncio.Queue()
        cancel = asyncio.Event()
        # Buffer the full turn's audio + prose so a clean (non-barge-in) turn can
        # be persisted as one replayable voice_messages row.
        pcm_parts: list[bytes] = []
        audio_rate: int | None = None
        final_text = ""

        async def _send_audio(pcm: bytes, rate: int) -> None:
            for i in range(0, len(pcm), _AUDIO_FRAME_BYTES):
                if cancel.is_set():
                    return
                frame = pcm[i : i + _AUDIO_FRAME_BYTES]
                # Ephemeral: fanned out live to every viewer, never ringed (a
                # turn's PCM is megabytes). The persisted WAV is the replay path.
                run.emit({
                    "type": "audio_chunk",
                    "pcm": base64.b64encode(frame).decode("ascii"),
                    "sample_rate": rate,
                })
                await asyncio.sleep(0)  # let the pumps drain between frames

        async def _tts_worker() -> None:
            nonlocal audio_rate, agent_speaking, speaking_since, speaking_ended_at
            spoke = False
            while True:
                sentence = await tts_queue.get()
                try:
                    if sentence is None:
                        break
                    if cancel.is_set():
                        continue
                    pcm, rate = await voice.synthesize(sentence)
                    if not pcm:
                        logger.warning(
                            "voice[%s]: TTS produced no audio for %r — check PIPER_MODEL / `say`",
                            origin, sentence[:80],
                        )
                    if pcm and not cancel.is_set():
                        if not spoke:
                            run.emit({"type": "speaking_start"})
                            spoke = True
                            # Enter speaking mode: stricter VAD onset + grace
                            # window so the agent's own TTS can't self-interrupt.
                            agent_speaking = True
                            speaking_since = time.monotonic()
                            if voice is not None:
                                voice.set_speaking(True)
                        await _send_audio(pcm, rate)
                        pcm_parts.append(pcm)
                        audio_rate = rate
                finally:
                    tts_queue.task_done()
            if spoke:
                run.emit({"type": "speaking_end"})
            agent_speaking = False
            speaking_ended_at = time.monotonic()
            if voice is not None:
                voice.set_speaking(False)

        worker = asyncio.create_task(_tts_worker())
        sink = _event_sink(run)

        async def _voice_on_event(ev: StreamEvent) -> None:
            nonlocal final_text
            await sink(ev)
            if ev.kind == "token":
                for sentence in chunker.feed(ev.data.get("text", "")):
                    await tts_queue.put(sentence)
            elif ev.kind == "final":
                final_text = ev.data.get("text", "") or final_text
                rest = chunker.flush()
                if rest:
                    await tts_queue.put(rest)

        try:
            await convo.run_turn(
                run.text,
                on_event=_voice_on_event,
                ask_handler=_ask_handler(run),
                run_name=f"{origin}-voice",
                keep_full_payloads=True,
                modality="voice",
            )
        except asyncio.CancelledError:
            cancel.set()
            raise
        finally:
            agent_active = False
            await tts_queue.put(None)
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            # Persist the assistant turn (text + replayable TTS audio) only when
            # it completed cleanly — a barge-in leaves a partial clip we discard.
            if voice_store is not None and not cancel.is_set() and (final_text or pcm_parts):
                await _persist_voice_message(
                    role="assistant",
                    modality="audio" if pcm_parts else "text",
                    text=final_text,
                    pcm=b"".join(pcm_parts) if pcm_parts else None,
                    rate=audio_rate,
                )

    # Continuous (hands-free) voice: the mic stays live across turns, so the
    # user can keep talking while the agent is mid-answer. A completed utterance
    # spoken during a running turn is queued (latest wins) and run the moment the
    # current turn ends, instead of being dropped — so nothing the user says is
    # lost and they never wait for the mic to "stop".
    queued_utterance: bytes | None = None
    # Transcription of a captured utterance. Genuinely per-connection (it feeds
    # on this socket's VoicePipeline) — only the agent turn it leads to is
    # handed to the daemon.
    utt_task: asyncio.Task | None = None

    def _utterance_pending() -> bool:
        return utt_task is not None and not utt_task.done()

    def _on_turn_done(run: TurnRun, task: asyncio.Task) -> None:
        nonlocal queued_utterance
        utt = queued_utterance
        queued_utterance = None
        if utt is None or task.cancelled():
            # A cancelled turn is a barge-in that already started a fresh turn;
            # don't double-run.
            return
        _spawn_utterance(utt)

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
            on_done=_on_turn_done,
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

    async def _voice_turn_from_utterance(pcm: bytes, *, preempt: bool = False) -> None:
        """Transcribe a completed utterance, echo it as the user turn, run it."""
        assert voice is not None
        logger.info("voice[%s]: utterance %d bytes → transcribing", origin, len(pcm))
        try:
            result = await voice.transcribe_detailed(pcm)
        except asyncio.CancelledError:
            raise
        text = result.text
        if not text:
            # Nothing intelligible — release the UI without a turn. Logged so a
            # "no response" symptom is traceable to empty STT vs. a failed turn.
            logger.info("voice[%s]: empty transcript — no turn run", origin)
            await _send({"type": "turn_end"})
            return
        # Confidence gate: a low-confidence transcript is likely garbled ASR
        # (the "welcome to the general" failure). Ask the user to repeat rather
        # than feed the agent a bad query. Only gates backends that report a
        # confidence; None (e.g. Groq) always passes.
        conf = result.confidence
        if conf is not None and _STT_MIN_CONFIDENCE > 0 and conf < _STT_MIN_CONFIDENCE:
            logger.info(
                "voice[%s]: low-confidence transcript (%.2f < %.2f) %r — asking to repeat",
                origin, conf, _STT_MIN_CONFIDENCE, text[:120],
            )
            await _send({
                "type": "clarify",
                "reason": "low_confidence",
                "confidence": round(conf, 3),
                "heard": text,
                "message": "I didn't quite catch that — could you say it again?",
            })
            await _send({"type": "turn_end"})
            return
        logger.info("voice[%s]: transcript=%r → running turn", origin, text[:120])
        # Mic artifacts stay on this socket; the run's `turn_start` frame carries
        # the same text to every other viewer.
        await _send({"type": "transcript", "text": text})
        # Persist the user side as text (the STT transcript). Raw user audio is
        # intentionally not stored by default — privacy + size.
        await _persist_voice_message(role="user", modality="text", text=text)
        await _start_spoken_turn(text, preempt=preempt)

    def _spawn_utterance(pcm: bytes, *, preempt: bool = False) -> None:
        """Transcribe-then-run, replacing any utterance still transcribing."""
        nonlocal utt_task
        if _utterance_pending():
            utt_task.cancel()
        utt_task = asyncio.create_task(_voice_turn_from_utterance(pcm, preempt=preempt))
        bg_tasks.add(utt_task)
        utt_task.add_done_callback(bg_tasks.discard)

    # True while a candidate barge-in utterance is being transcribed, so we don't
    # evaluate several overlapping talk-over guesses at once.
    barge_pending = False

    async def _maybe_barge_in(pcm: bytes) -> None:
        """Interrupt the agent's spoken reply only for genuine talk-over.

        Called when a full utterance is captured *while the agent is speaking*.
        Instead of cancelling the reply on a raw VAD onset (which room noise or
        the agent's own TTS echo trips constantly — the reply cutting out
        mid-sentence), we transcribe the utterance first and only interrupt when
        it yields real, confident words. Otherwise the agent is left to finish.
        """
        nonlocal barge_pending
        if not _BARGE_IN_ENABLED or barge_pending or voice is None:
            return
        barge_pending = True
        try:
            try:
                result = await voice.transcribe_detailed(pcm)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("voice[%s]: barge transcription failed", origin, exc_info=True)
                return
            text = (result.text or "").strip()
            if not text:
                logger.info("voice[%s]: talk-over ignored — noise/echo while speaking", origin)
                return
            conf = result.confidence
            if conf is not None and _STT_MIN_CONFIDENCE > 0 and conf < _STT_MIN_CONFIDENCE:
                logger.info(
                    "voice[%s]: talk-over ignored — low confidence (%.2f) %r",
                    origin, conf, text[:80],
                )
                return
            # Real talk-over: stop the agent's audio + turn, then answer the new
            # utterance. speech_started tells the client to drop the stale audio.
            logger.info("voice[%s]: barge-in confirmed → %r", origin, text[:80])
            await _send({"type": "speech_started"})
            await _send({"type": "transcript", "text": text})
            await _persist_voice_message(role="user", modality="text", text=text)
            await _start_spoken_turn(text, preempt=True)
        finally:
            barge_pending = False

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
                # Route through the shared DecisionService so an answer given
                # here is indistinguishable from one given in the Inbox or the
                # overlay: same record, same resolution, same ask_resolved
                # broadcast clearing every other surface.
                if ask_id and decisions is not None:
                    from yuyutsava.daemon.web.services.decision_service import (
                        DecisionConflictError,
                    )
                    try:
                        await decisions.respond_ask(str(ask_id), text)
                    except DecisionConflictError:
                        # Someone answered it elsewhere first — that's the
                        # design, not an error. Tell this socket so it clears.
                        await _send({"type": "ask_resolved", "ask_id": ask_id})
                    continue
                # No hub (tests / embedded): the run-local future is the path.
                registry.answer_ask(thread_id, text)
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
                        or _utterance_pending()
                        or agent_active
                        or agent_speaking
                        or (time.monotonic() - speaking_ended_at) < _POST_SPEAK_GRACE_SEC
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
                    if not agent_speaking:
                        await _send({"type": "speech_started"})
                if res.utterance is not None:
                    if agent_speaking:
                        # Possible talk-over. Don't cancel yet — verify it's real
                        # speech (transcribe-before-cancel) so noise/echo can't chop
                        # the reply off. Runs off to the side while the agent keeps
                        # speaking; only confirmed words actually interrupt.
                        _spawn_bg(_maybe_barge_in(res.utterance))
                    elif agent_active or turn_running:
                        # The agent is mid-answer (thinking) — don't interrupt it,
                        # but don't drop what the user just said either. Queue this
                        # completed utterance (latest wins) so it runs as the next
                        # turn the moment the current one ends. Keeps the mic
                        # continuously useful without killing the in-flight answer.
                        queued_utterance = res.utterance
                        logger.info(
                            "voice[%s]: utterance queued — runs after current turn",
                            origin,
                        )
                    else:
                        # Idle, or only transcribing a prior (possibly-empty noise)
                        # utterance — let this newer utterance win: the spawn
                        # cancels the stale transcription and runs this one.
                        _spawn_utterance(res.utterance)
                continue

            if mtype == "audio_end":
                # Explicit end of capture (push-to-talk release): flush the VAD.
                if voice is not None:
                    utt = voice.flush()
                    if utt:
                        _spawn_utterance(utt, preempt=True)
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
