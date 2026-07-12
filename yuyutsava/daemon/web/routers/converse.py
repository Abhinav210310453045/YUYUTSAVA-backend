"""WebSocket transport for interactive agent conversations (text + voice).

``WS /ws/converse`` is the bidirectional channel the Electron/mobile UIs use to
talk to the daemon-hosted deepagent (see
:class:`yuyutsava.daemon.conversation_manager.ConversationManager`). It is the
non-CLI sibling of the terminal REPL: same :class:`ConversationService`, same
turn loop, different transport.

Protocol (JSON text frames)
---------------------------
Client → server:
  * ``{"type":"user_text","text":...}``   — run one turn (text)
  * ``{"type":"audio","pcm":<base64>}``   — a frame of 16 kHz mono int16 mic PCM
  * ``{"type":"audio_end"}``              — end of capture (push-to-talk release)
  * ``{"type":"ask_response","text":...}`` — answer a pending HITL interrupt
  * ``{"type":"interrupt"}``               — cancel the in-flight turn (barge-in)
  * ``{"type":"ping"}``

Server → client:
  * ``{"type":"hello","session_id":...,"thread_id":...,"resuming":bool}``
  * StreamEvent frames: ``token`` / ``tool_call`` / ``tool_result`` / ``image`` / ``log`` / ``final``
  * ``{"type":"speech_started"}``          — VAD detected the user talking
  * ``{"type":"transcript","text":...}``   — final STT of the user's utterance
  * ``{"type":"speaking_start"}`` / ``{"type":"speaking_end"}`` — TTS bracketing
  * ``{"type":"audio_chunk","pcm":<base64>,"sample_rate":int}`` — spoken reply PCM
  * ``{"type":"ask","payload":{...}}``     — interrupt; reply with ``ask_response``
  * ``{"type":"turn_end"}``                — turn finished
  * ``{"type":"error","message":...}``

The voice loop: mic ``audio`` frames are segmented by VAD; a completed utterance
is transcribed (``transcript``) and run as a turn whose prose is streamed back as
``audio_chunk`` frames. Speaking while the agent talks (``speech_started`` during
a turn) cancels it — barge-in.

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

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from yuyutsava.audio_io.blobs import write_voice_wav
from yuyutsava.audio_io.sentence import SentenceChunker
from yuyutsava.core.streaming import StreamEvent
from yuyutsava.daemon.web.voice_pipeline import VoicePipeline

logger = logging.getLogger("yuyutsava.daemon.web.routers.converse")

router = APIRouter(tags=["converse"])

# Cap on how long an inline HITL ask waits for the user before auto-rejecting,
# so a walked-away user can't pin an agent turn open forever.
_ASK_TIMEOUT_SEC = 300.0

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


@router.websocket("/ws/converse")
async def converse(ws: WebSocket) -> None:
    manager = getattr(ws.app.state, "conversation_manager", None)
    await ws.accept()
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
    # agent=tinker&card=<card_id> routes to the card-bound TinkerAgent bundle,
    # thread pinned to todo:<card_id>. Default is the shared master deepagent.
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

    logger.info(
        "converse: WS open agent=%s card=%s origin=%s resume_id=%s → session=%s "
        "resuming=%s voice_barge_in=%s (post_speak_grace=%.1fs)",
        agent, card_id, origin, resume_id, convo.session_id, resuming,
        _BARGE_IN_ENABLED, _POST_SPEAK_GRACE_SEC,
    )
    await ws.send_text(json.dumps({
        "type": "hello",
        "session_id": convo.session_id,
        "thread_id": convo.thread_id,
        "origin": origin,
        "agent": agent,
        "card_id": card_id,
        "resuming": resuming,
        # Tell the client the voice interruption policy so it can match: when
        # barge-in is off (default) the client runs half-duplex — it mutes the
        # mic while the reply is still playing so noise can't cut it off.
        "barge_in": _BARGE_IN_ENABLED,
    }))

    # Shared per-connection state, mutated by the single receive loop below.
    pending_ask: asyncio.Future[str] | None = None
    turn_task: asyncio.Task | None = None
    voice: VoicePipeline | None = None  # built lazily on first audio frame
    send_lock = asyncio.Lock()
    # Background best-effort tasks (model pre-warm) kept referenced so they aren't
    # GC'd mid-flight; cancelled on disconnect.
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
    agent_speaking = False
    speaking_since = 0.0
    speaking_ended_at = 0.0
    # True while an ACTUAL agent turn is executing (a non-empty transcript has
    # started run_turn), as opposed to merely transcribing an utterance that may
    # turn out to be empty noise. The audio handler protects an active turn from
    # being cancelled by trailing words / noise, but still lets a fresh utterance
    # replace a turn that's only transcribing.
    agent_active = False

    async def _send(obj: dict) -> None:
        async with send_lock:
            await ws.send_text(json.dumps(obj, default=str))

    async def _on_event(ev: StreamEvent) -> None:
        await _send({"type": ev.kind, **ev.data})

    async def _ask_handler(interrupt_value) -> str:
        nonlocal pending_ask
        payload = (
            interrupt_value if isinstance(interrupt_value, dict)
            else {"text": str(interrupt_value)}
        )
        loop = asyncio.get_running_loop()
        pending_ask = loop.create_future()
        await _send({"type": "ask", "payload": payload})
        try:
            return await asyncio.wait_for(pending_ask, timeout=_ASK_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            return "reject"
        finally:
            pending_ask = None

    async def _run_turn(text: str) -> None:
        nonlocal agent_active
        agent_active = True
        try:
            if not convo.bundle_ready:
                await _send({"type": "log", "text": "preparing agent (first run)…"})
            await convo.run_turn(
                text,
                on_event=_on_event,
                ask_handler=_ask_handler,
                run_name=f"{origin}-chat",
                keep_full_payloads=True,
            )
        except asyncio.CancelledError:
            await _send({"type": "log", "text": "(turn cancelled)"})
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("converse: turn failed", exc_info=True)
            await _send({"type": "error", "message": str(exc)})
        finally:
            agent_active = False
            await _send({"type": "turn_end"})

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

    async def _run_voice_turn(text: str) -> None:
        """Run a turn whose prose is also spoken back as streamed audio.

        Agent tokens are accumulated into sentences and synthesized one at a
        time on a side queue, so audio starts on sentence 1 while the model is
        still writing. Cancellation (barge-in / interrupt) stops both the turn
        and any queued/!in-flight TTS immediately.
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
                await _send({
                    "type": "audio_chunk",
                    "pcm": base64.b64encode(frame).decode("ascii"),
                    "sample_rate": rate,
                })

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
                            await _send({"type": "speaking_start"})
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
                await _send({"type": "speaking_end"})
            agent_speaking = False
            speaking_ended_at = time.monotonic()
            if voice is not None:
                voice.set_speaking(False)

        worker = asyncio.create_task(_tts_worker())

        async def _voice_on_event(ev: StreamEvent) -> None:
            nonlocal final_text
            await _on_event(ev)
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
                text,
                on_event=_voice_on_event,
                ask_handler=_ask_handler,
                run_name=f"{origin}-voice",
                keep_full_payloads=True,
                modality="voice",
            )
        except asyncio.CancelledError:
            cancel.set()
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("converse: voice turn failed", exc_info=True)
            await _send({"type": "error", "message": str(exc)})
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
            await _send({"type": "turn_end"})

    async def _voice_turn_from_utterance(pcm: bytes) -> None:
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
        await _send({"type": "transcript", "text": text})
        # Persist the user side as text (the STT transcript). Raw user audio is
        # intentionally not stored by default — privacy + size.
        await _persist_voice_message(role="user", modality="text", text=text)
        await _run_voice_turn(text)

    # Continuous (hands-free) voice: the mic stays live across turns, so the
    # user can keep talking while the agent is mid-answer. A completed utterance
    # spoken during a running turn is queued (latest wins) and run the moment the
    # current turn ends, instead of being dropped — so nothing the user says is
    # lost and they never wait for the mic to "stop".
    queued_utterance: bytes | None = None

    def _on_turn_done(t: asyncio.Task) -> None:
        nonlocal queued_utterance
        utt = queued_utterance
        queued_utterance = None
        if utt is None or t.cancelled():
            # A cancelled turn is a barge-in that already started a fresh turn;
            # don't double-run.
            return
        _spawn_turn(_voice_turn_from_utterance(utt))

    def _spawn_turn(coro) -> None:
        nonlocal turn_task
        turn_task = asyncio.create_task(coro)
        turn_task.add_done_callback(_on_turn_done)

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
            if turn_task is not None and not turn_task.done():
                turn_task.cancel()
            await _send({"type": "transcript", "text": text})
            await _persist_voice_message(role="user", modality="text", text=text)
            _spawn_turn(_run_voice_turn(text))
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
                if pending_ask is not None and not pending_ask.done():
                    pending_ask.set_result(str(msg.get("text", "")))
                continue

            if mtype == "interrupt":
                if turn_task is not None and not turn_task.done():
                    turn_task.cancel()
                continue

            if mtype == "user_text":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                if turn_task is not None and not turn_task.done():
                    # One turn at a time; a stray send while busy is ignored.
                    await _send({"type": "error", "message": "a turn is already running"})
                    continue
                turn_task = asyncio.create_task(_run_turn(text))
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
                turn_running = turn_task is not None and not turn_task.done()
                if not _BARGE_IN_ENABLED:
                    turn_active = (
                        turn_running
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
                    elif agent_active:
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
                        # utterance — let this newer utterance win: cancel the
                        # stale transcription and run this one.
                        if turn_running:
                            turn_task.cancel()
                        _spawn_turn(_voice_turn_from_utterance(res.utterance))
                continue

            if mtype == "audio_end":
                # Explicit end of capture (push-to-talk release): flush the VAD.
                if voice is not None:
                    utt = voice.flush()
                    if utt:
                        if turn_task is not None and not turn_task.done():
                            turn_task.cancel()
                        _spawn_turn(_voice_turn_from_utterance(utt))
                continue

            await _send({"type": "error", "message": f"unknown message type {mtype!r}"})

    except WebSocketDisconnect:
        pass
    finally:
        for t in list(bg_tasks):
            if not t.done():
                t.cancel()
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
            try:
                await turn_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if voice is not None:
            voice.close()
        # A session opened but never used (tab/overlay connect with nothing said,
        # or a flaky reconnect) is just clutter — drop it so the Sessions history
        # doesn't fill with empty seconds-old rows. Only freshly-created sessions
        # qualify; a resumed one is left alone. Otherwise mark it idle (not done)
        # so a reconnect (--resume) can continue it. Best-effort.
        try:
            discarded = (not resuming) and await convo.discard_if_unused()
            if not discarded:
                await convo.finish("idle")
        except Exception:  # noqa: BLE001
            pass
