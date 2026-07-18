import { useCallback, useEffect, useRef, useState } from 'react'
import { ConverseClient } from '../api/converse'
import { getBase, getSessionMessages } from '../api/client'
import { TokenSmoother } from '../lib/tokenSmoother'
import { audioPlayer, base64ToInt16 } from '../audio'
import { MicCapture } from '../audio/capture'

// Shared conversation state machine over WS /ws/converse. Used by the text
// ChatPanel and (later) the voice UI — both speak the same protocol; voice just
// layers audio capture/playback on top. Returns a small, transport-agnostic API.
//
// message shape: { id, role: 'user'|'assistant', text, events: [...], streaming,
//                  images: [{visual_id, url, kind, title}], feedback: 'up'|'down' }
let _mid = 0
const nextId = () => `m${++_mid}`

// `agent`/`card` select the server-side bundle (agent='tinker' + a card id
// pins the thread to that TODO card); omitted → the master deepagent.
export function useConverse({ origin = 'cli', resumeId = null, agent = null, card = null } = {}) {
  const [messages, setMessages] = useState([])
  const [connected, setConnected] = useState(false)
  const [busy, setBusy] = useState(false)
  const [pendingAsk, setPendingAsk] = useState(null) // { payload }
  const [hello, setHello] = useState(null)            // { session_id, thread_id, ... }
  const [listening, setListening] = useState(false)   // mic capture active
  const [speaking, setSpeaking] = useState(false)     // agent TTS playing
  // Id of the message whose spoken audio is currently audible — the live reply
  // while it plays, or a past turn while it's being replayed. Drives the
  // play↔stop toggle on the voice bubbles so a long clip can be cut off.
  const [playingId, setPlayingId] = useState(null)
  // True while the audible clip is user-paused (context suspended, position
  // held). Drives the pause↔resume toggle on chat bubbles.
  const [paused, setPaused] = useState(false)
  // The thread we actually connect to. Seeded from the `resumeId` prop (used
  // when opening a past session from history) but overridable in-place: the
  // per-view "New" button clears it to start a brand-new thread without
  // remounting the panel. `resetNonce` forces a reconnect even when the id is
  // unchanged (e.g. New pressed on an already-fresh chat).
  const [activeResumeId, setActiveResumeId] = useState(resumeId)
  const [resetNonce, setResetNonce] = useState(0)
  const clientRef = useRef(null)
  const micRef = useRef(null)
  // Latest playingId (for stale-closure-free reads in callbacks) + the interval
  // that watches the shared audioPlayer and flips the toggle back to ▶ when the
  // clip drains on its own.
  const playingIdRef = useRef(null)
  const playPollRef = useRef(null)
  // Server's barge-in policy (from the hello frame). Off by default → half-duplex:
  // the mic is muted while the agent's reply is playing.
  const bargeInRef = useRef(false)
  // Id of the assistant message currently being streamed into.
  const streamingId = useRef(null)
  // Paces incoming prose tokens to a smooth typewriter cadence (mirrors the CLI
  // TokenSmoother) instead of dumping jumpy chunks. One per streaming message.
  const smootherRef = useRef(null)

  const appendToStreaming = useCallback((patch) => {
    setMessages((cur) => {
      const id = streamingId.current
      if (!id) return cur
      return cur.map((m) => (m.id === id ? patch(m) : m))
    })
  }, [])

  // Mark the streaming message done and stop pointing at it. Captures the id
  // NOW and patches by that explicit id — must not go through appendToStreaming,
  // whose updater reads streamingId.current at flush time (after we've nulled it),
  // which would silently drop the streaming:false patch and leave the typing
  // dots spinning + hide the message actions forever.
  const finalizeStreaming = useCallback(() => {
    const id = streamingId.current
    streamingId.current = null
    if (id) setMessages((cur) => cur.map((m) => (m.id === id ? { ...m, streaming: false } : m)))
  }, [])

  // Reveal buffered prose immediately — call before rendering any non-prose
  // (tool/log) event so text stays in correct order, and at end of stream.
  const flushSmoother = useCallback(() => {
    smootherRef.current?.flush()
  }, [])

  // Tear down the smoother (drop pending text) at turn boundaries / teardown.
  const stopSmoother = useCallback(() => {
    smootherRef.current?.stop()
    smootherRef.current = null
  }, [])

  const feedSmoother = useCallback((text) => {
    if (!smootherRef.current) {
      smootherRef.current = new TokenSmoother(
        (chunk) => appendToStreaming((m) => ({ ...m, text: m.text + chunk })),
      )
    }
    smootherRef.current.feed(text)
  }, [appendToStreaming])

  // Mark `id` as the audible message and (re)arm a poll that flips the toggle
  // back once the shared player drains. Called on every audio chunk of a live
  // reply and once when a replay is queued — the frequent re-affirm keeps the
  // Stop button steady across the tiny gaps between streamed chunks, while the
  // poll clears it the moment the audio truly finishes.
  const watchPlayback = useCallback((id) => {
    playingIdRef.current = id
    setPlayingId(id)
    if (playPollRef.current) return
    playPollRef.current = setInterval(() => {
      if (!audioPlayer.isPlaying()) {
        clearInterval(playPollRef.current)
        playPollRef.current = null
        playingIdRef.current = null
        setPlayingId(null)
      }
    }, 250)
  }, [])

  // Cut off whatever is playing and reset the toggle to ▶. audioPlayer.stop()
  // also clears a pending pause (a suspended context would mute future audio).
  const stopPlayback = useCallback(() => {
    if (playPollRef.current) { clearInterval(playPollRef.current); playPollRef.current = null }
    audioPlayer.stop()
    playingIdRef.current = null
    setPlayingId(null)
    setPaused(false)
  }, [])

  // Pause / resume the audible clip in place (live reply or replay): the audio
  // clock freezes, so playback resumes exactly where it stopped. No-op when
  // nothing is playing.
  const togglePause = useCallback(async () => {
    if (audioPlayer.isPaused()) {
      await audioPlayer.resume()
      setPaused(false)
    } else if (audioPlayer.isPlaying()) {
      await audioPlayer.pause()
      setPaused(true)
    }
  }, [])

  const onMessage = useCallback((msg) => {
    switch (msg.type) {
      case 'hello':
        setHello(msg)
        // Whether the server allows voice barge-in (talk-over). Default off:
        // while off we mute the mic and ignore interrupt-y events until the
        // reply finishes PLAYING, so background noise can't cut it off.
        bargeInRef.current = !!msg.barge_in
        // Pin the live thread so a reconnect (socket drop / daemon restart)
        // resumes THIS conversation instead of starting a fresh one — keeps the
        // chat history (typed + spoken turns) intact across drops.
        if (msg.thread_id) clientRef.current?.setResumeId(msg.thread_id)
        break
      case 'token': {
        if (!streamingId.current) {
          const id = nextId()
          streamingId.current = id
          setMessages((cur) => [...cur, { id, role: 'assistant', text: '', events: [], images: [], artifacts: [], streaming: true }])
        }
        feedSmoother(msg.text || '')
        break
      }
      case 'image': {
        // Inline artifact (chart/diagram/table/…) rendered by a vis_* tool this
        // turn. Attach it to the streaming assistant message so it renders inside
        // the bubble; create the message if the image somehow arrives first.
        flushSmoother()
        if (!streamingId.current) {
          const id = nextId()
          streamingId.current = id
          setMessages((cur) => [...cur, { id, role: 'assistant', text: '', events: [], images: [], artifacts: [], streaming: true }])
        }
        appendToStreaming((m) => ({
          ...m,
          images: [...(m.images || []), {
            visual_id: msg.visual_id, url: msg.url, kind: msg.kind, title: msg.title, mime: msg.mime,
          }],
        }))
        break
      }
      case 'artifact': {
        // Inline rich artifact (interactive HTML/JSX, doc, audio) made by
        // artifact_create this turn — attach it to the streaming assistant
        // message so it renders as a block card in the bubble, openable big.
        flushSmoother()
        if (!streamingId.current) {
          const id = nextId()
          streamingId.current = id
          setMessages((cur) => [...cur, { id, role: 'assistant', text: '', events: [], images: [], artifacts: [], streaming: true }])
        }
        appendToStreaming((m) => ({
          ...m,
          artifacts: [...(m.artifacts || []), {
            attachment_id: msg.attachment_id || msg.artifact_id,
            url: msg.url, kind: msg.kind, mime: msg.mime, title: msg.title,
          }],
        }))
        break
      }
      case 'tool_call':
        flushSmoother() // show buffered prose before the tool row
        appendToStreaming((m) => ({ ...m, events: [...m.events, { kind: 'tool_call', name: msg.name, args: msg.args }] }))
        break
      case 'tool_result':
        flushSmoother()
        appendToStreaming((m) => ({ ...m, events: [...m.events, { kind: 'tool_result', name: msg.name, preview: msg.preview }] }))
        break
      case 'log':
        // surface as a transient event on the streaming message (or ignore)
        flushSmoother()
        appendToStreaming((m) => ({ ...m, events: [...m.events, { kind: 'log', text: msg.text }] }))
        break
      case 'final':
        // Drain buffered prose, then snap to the canonical final text. A
        // correct final is always ≥ the streamed text (same chunks), so a
        // SHORTER final can only be a server-side truncation — keep the
        // accumulated text rather than snapping the bubble back to a stub.
        flushSmoother()
        if (msg.text) appendToStreaming((m) => (
          msg.text.length >= m.text.length ? { ...m, text: msg.text } : m
        ))
        break
      case 'ask':
        setPendingAsk({ payload: msg.payload || {} })
        break
      case 'speech_started':
        // The user is talking — cut off any agent audio still playing (barge-in).
        // But with barge-in off, a "speech onset" during playback is almost
        // always the agent's own audio echo / room noise: DON'T stop a reply
        // that's still playing, or it gets chopped mid-sentence. A real user
        // interrupt goes through the Stop button (interrupt()).
        if (bargeInRef.current || !audioPlayer.isPlaying()) stopPlayback()
        break
      case 'transcript':
        // STT of the user's spoken utterance becomes the user turn.
        setMessages((cur) => [...cur, { id: nextId(), role: 'user', text: msg.text, events: [] }])
        setBusy(true)
        break
      case 'audio_chunk':
        try {
          const pcm = base64ToInt16(msg.pcm)
          const sr = msg.sample_rate || 22050
          audioPlayer.enqueuePcm(pcm, sr)
          // Retain the chunk on the streaming assistant message so the Voice UI
          // can offer a ▶ replay of this turn's spoken reply (in-session only;
          // cross-restart persistence is Phase 6).
          appendToStreaming((m) => ({ ...m, audioChunks: [...(m.audioChunks || []), pcm], audioSampleRate: sr }))
          // The live reply is now audible — light up this message's Stop toggle
          // and let the poll flip it back to ▶ when playback finishes draining.
          if (streamingId.current) watchPlayback(streamingId.current)
        } catch { /* ignore */ }
        break
      case 'speaking_start':
        setSpeaking(true)
        break
      case 'speaking_end':
        setSpeaking(false)
        break
      case 'turn_end':
        flushSmoother()
        stopSmoother()
        finalizeStreaming()
        setBusy(false)
        setSpeaking(false)
        setPendingAsk(null)
        break
      case 'clarify':
        // Low-confidence ASR: the agent turn was skipped on purpose. Show a
        // gentle re-prompt instead of running on a garbled transcript. A
        // turn_end follows to release the busy/speaking state. Never let a stray
        // low-confidence "utterance" (usually background noise) stop a reply
        // that's still playing.
        if (bargeInRef.current || !audioPlayer.isPlaying()) stopPlayback()
        setMessages((cur) => [...cur, {
          id: nextId(),
          role: 'assistant',
          text: msg.message || "I didn't quite catch that — could you say it again?",
          events: [],
          clarify: true,
        }])
        break
      case 'error':
        flushSmoother()
        stopSmoother()
        stopPlayback()
        setMessages((cur) => [...cur, { id: nextId(), role: 'assistant', text: `⚠ ${msg.message}`, events: [], error: true }])
        finalizeStreaming()
        setBusy(false)
        setSpeaking(false)
        break
      default:
        break
    }
  }, [appendToStreaming, finalizeStreaming, feedSmoother, flushSmoother, stopSmoother, watchPlayback, stopPlayback])

  // Keep the connected thread in sync when the caller changes the prop (e.g.
  // opening a specific past session from the Sessions list).
  useEffect(() => { setActiveResumeId(resumeId) }, [resumeId])

  useEffect(() => {
    // Fresh transport per origin/thread — clear any prior thread's bubbles.
    setMessages([])
    streamingId.current = null
    setBusy(false)
    setPendingAsk(null)

    // Resume-history render (Phase 6b): hydrate the past turns of a resumed
    // thread so it doesn't open empty. Voice turns carry an audio_url for ▶
    // replay; text chats are prose only. Runs alongside the WS connect — the
    // user hasn't sent anything yet, so there's no ordering race with new turns.
    let cancelled = false
    if (activeResumeId) {
      getSessionMessages(activeResumeId)
        .then((res) => {
          if (cancelled || !res?.messages?.length) return
          setMessages(res.messages.map((m) => ({
            id: nextId(),
            role: m.role,
            // The verbatim transcript stores the server-composed selection
            // block ahead of what the user typed — strip it on hydration so
            // resumed threads match what the live bubble showed.
            text: m.role === 'user'
              ? (m.text || '').replace(/^<selection-context>\n[\s\S]*?\n<\/selection-context>\n\n/, '')
              : (m.text || ''),
            events: [],
            // Inline artifact cards made during the turn — the history endpoint
            // rebuilds them from the transcript's artifact_create tool results.
            artifacts: m.artifacts || [],
            // The server's audio_url is authoritative — on mixed text+voice
            // threads it carries the VOICE-store seq, which differs from this
            // row's transcript seq (rebuilding from m.seq would 404).
            audioUrl: m.audio_url ? `${getBase()}${m.audio_url}` : null,
          })))
        })
        .catch(() => { /* history is best-effort — a fresh thread is fine */ })
    }

    const client = new ConverseClient(
      {
        onMessage,
        onConnected: () => setConnected(true),
        onDisconnected: () => {
          setConnected(false)
          // A dropped socket can never finish the turn — release the input and
          // surface whatever prose was buffered before the drop. Also finalize
          // any message still mid-stream so it doesn't stay stuck (no
          // copy/retry, endless typing dots) — see finalizeStreaming's own note
          // on why this can't reuse appendToStreaming.
          flushSmoother()
          stopSmoother()
          finalizeStreaming()
          setBusy(false)
          setPendingAsk(null)
        },
        // The client gave up resuming a dead session (server rejected it
        // MAX_RESUME_FAILURES times in a row before any `hello`, e.g. a
        // freshly-created chat that got discarded after an early disconnect).
        // Surface exactly one clear message instead of the storm of raw
        // rejections that led here — the user picks "New chat" or a different
        // history entry to recover; we don't silently start one for them.
        onResumeExhausted: (message) => {
          console.warn('chat resume failed permanently:', message)
          setConnected(false)
          setMessages((cur) => [...cur, {
            id: nextId(), role: 'assistant', error: true, events: [],
            text: '⚠ this chat is no longer available — start a new one',
          }])
          flushSmoother()
          stopSmoother()
          finalizeStreaming()
          setBusy(false)
          setPendingAsk(null)
        },
      },
      { origin, resumeId: activeResumeId, agent, card },
    )
    clientRef.current = client
    client.connect()
    return () => { cancelled = true; client.disconnect(); stopSmoother(); clientRef.current = null }
  }, [origin, activeResumeId, resetNonce, agent, card, onMessage, flushSmoother, stopSmoother, finalizeStreaming])

  // `context` (optional) carries board-selection references invisibly — the
  // local bubble and transcript render only the typed text. Returns whether
  // the frame actually left, so callers can consume one-shot context (chips)
  // only on success.
  const send = useCallback((text, { context } = {}) => {
    const t = (text || '').trim()
    if (!t || busy) return false
    const ok = clientRef.current?.sendText(t, context || null)
    if (!ok) {
      setMessages((cur) => [...cur, { id: nextId(), role: 'assistant', error: true, events: [],
        text: '⚠ not connected to the daemon — is it running? (restart it after updates)' }])
      return false
    }
    setMessages((cur) => [...cur, { id: nextId(), role: 'user', text: t, events: [] }])
    setBusy(true)
    return true
  }, [busy])

  const answerAsk = useCallback((text) => {
    clientRef.current?.answerAsk(text)
    setPendingAsk(null)
  }, [])

  // ---- voice (mic capture) --------------------------------------------
  const startVoice = useCallback(async () => {
    if (micRef.current) return
    // ensureContext() must run from a user gesture to unlock playback.
    try { await audioPlayer.ensureContext() } catch { /* ignore */ }
    // Half-duplex mic: while the agent's reply is still PLAYING (client-side
    // playback lags server synthesis by seconds), don't stream mic frames to the
    // server — otherwise its own audio echo / room noise forms a "new utterance"
    // that interrupts the reply. Resumes the instant playback drains. When the
    // server enables barge-in, stream continuously so talk-over works.
    const mic = new MicCapture({
      onFrame: (int16) => {
        if (!bargeInRef.current && audioPlayer.isPlaying()) return
        clientRef.current?.sendAudio(int16)
      },
    })
    micRef.current = mic
    try {
      await mic.start()
      setListening(true)
    } catch (e) {
      micRef.current = null
      setListening(false)
      setMessages((cur) => [...cur, { id: nextId(), role: 'assistant', error: true, events: [],
        text: `⚠ microphone unavailable: ${e?.message || e}` }])
    }
  }, [])

  const stopVoice = useCallback(async () => {
    const mic = micRef.current
    micRef.current = null
    setListening(false)
    if (mic) {
      try { clientRef.current?.endAudio() } catch { /* ignore */ }
      await mic.stop()
    }
  }, [])

  // Stop the mic when the conversation transport tears down (unmount / thread
  // switch) so we never leave a hot mic behind.
  useEffect(() => () => {
    micRef.current?.stop(); micRef.current = null
    if (playPollRef.current) { clearInterval(playPollRef.current); playPollRef.current = null }
  }, [])

  // Play / stop a turn's spoken reply. Acts as a toggle: clicking the control on
  // the message that's currently audible cuts it off (so a long clip isn't a
  // hostage). In-session turns retain raw PCM chunks; a resumed turn instead
  // carries an audio_url to the persisted WAV (Phase 6b).
  const replay = useCallback(async (message) => {
    if (playingIdRef.current === message.id) { stopPlayback(); return }
    setPaused(false) // audioPlayer.stop() below clears the player-side pause
    try { await audioPlayer.ensureContext() } catch { /* ignore */ }
    const chunks = message?.audioChunks
    if (chunks && chunks.length > 0) {
      audioPlayer.stop()
      const total = chunks.reduce((n, c) => n + c.length, 0)
      const merged = new Int16Array(total)
      let off = 0
      for (const c of chunks) { merged.set(c, off); off += c.length }
      await audioPlayer.enqueuePcm(merged, message.audioSampleRate || 22050)
      watchPlayback(message.id)
      return
    }
    if (message?.audioUrl) {
      audioPlayer.stop()
      try { await audioPlayer.playUrl(message.audioUrl); watchPlayback(message.id) } catch { /* ignore */ }
    }
  }, [stopPlayback, watchPlayback])

  const interrupt = useCallback(() => {
    clientRef.current?.interrupt()
    stopPlayback()
    flushSmoother()
    stopSmoother()
    // Fallback: if the server doesn't ack within a moment (e.g. socket wedged),
    // release the UI so the user isn't stuck on "working".
    setTimeout(() => {
      finalizeStreaming()
      setBusy(false)
    }, 1500)
  }, [finalizeStreaming, flushSmoother, stopSmoother, stopPlayback])

  // Start a brand-new conversation in-place (per-view "New" button): stop any
  // audio/mic, drop the pinned/resumed thread, and reconnect fresh. The panel
  // stays mounted, so the rest of the app is untouched.
  const newSession = useCallback(() => {
    stopPlayback()
    flushSmoother()
    stopSmoother()
    if (micRef.current) { micRef.current.stop(); micRef.current = null }
    setListening(false)
    setSpeaking(false)
    setActiveResumeId(null)
    setResetNonce((n) => n + 1)
  }, [flushSmoother, stopSmoother, stopPlayback])

  // Live mic AnalyserNode for the Voice visualizer (null when not capturing).
  // The agent-speaking analyser lives on the shared audioPlayer singleton.
  const getMicAnalyser = useCallback(() => micRef.current?.getAnalyser() || null, [])

  return {
    messages, connected, busy, pendingAsk, hello,
    listening, speaking, playingId, paused,
    send, answerAsk, interrupt, startVoice, stopVoice, replay, togglePause, newSession,
    getMicAnalyser,
  }
}
