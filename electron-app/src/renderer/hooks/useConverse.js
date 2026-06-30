import { useCallback, useEffect, useRef, useState } from 'react'
import { ConverseClient } from '../api/converse'
import { getSessionMessages, sessionAudioUrl } from '../api/client'
import { TokenSmoother } from '../lib/tokenSmoother'
import { audioPlayer, base64ToInt16 } from '../audio'
import { MicCapture } from '../audio/capture'

// Shared conversation state machine over WS /ws/converse. Used by the text
// ChatPanel and (later) the voice UI — both speak the same protocol; voice just
// layers audio capture/playback on top. Returns a small, transport-agnostic API.
//
// message shape: { id, role: 'user'|'assistant', text, events: [...], streaming }
let _mid = 0
const nextId = () => `m${++_mid}`

export function useConverse({ origin = 'cli', resumeId = null } = {}) {
  const [messages, setMessages] = useState([])
  const [connected, setConnected] = useState(false)
  const [busy, setBusy] = useState(false)
  const [pendingAsk, setPendingAsk] = useState(null) // { payload }
  const [hello, setHello] = useState(null)            // { session_id, thread_id, ... }
  const [listening, setListening] = useState(false)   // mic capture active
  const [speaking, setSpeaking] = useState(false)     // agent TTS playing
  const clientRef = useRef(null)
  const micRef = useRef(null)
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

  const onMessage = useCallback((msg) => {
    switch (msg.type) {
      case 'hello':
        setHello(msg)
        // Pin the live thread so a reconnect (socket drop / daemon restart)
        // resumes THIS conversation instead of starting a fresh one — keeps the
        // chat history (typed + spoken turns) intact across drops.
        if (msg.thread_id) clientRef.current?.setResumeId(msg.thread_id)
        break
      case 'token': {
        if (!streamingId.current) {
          const id = nextId()
          streamingId.current = id
          setMessages((cur) => [...cur, { id, role: 'assistant', text: '', events: [], streaming: true }])
        }
        feedSmoother(msg.text || '')
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
        // Drain buffered prose, then snap to the canonical final text.
        flushSmoother()
        if (msg.text) appendToStreaming((m) => ({ ...m, text: msg.text }))
        break
      case 'ask':
        setPendingAsk({ payload: msg.payload || {} })
        break
      case 'speech_started':
        // The user is talking — cut off any agent audio still playing (barge-in).
        audioPlayer.stop()
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
        if (streamingId.current) appendToStreaming((m) => ({ ...m, streaming: false }))
        streamingId.current = null
        setBusy(false)
        setSpeaking(false)
        setPendingAsk(null)
        break
      case 'error':
        flushSmoother()
        stopSmoother()
        audioPlayer.stop()
        setMessages((cur) => [...cur, { id: nextId(), role: 'assistant', text: `⚠ ${msg.message}`, events: [], error: true }])
        if (streamingId.current) appendToStreaming((m) => ({ ...m, streaming: false }))
        streamingId.current = null
        setBusy(false)
        setSpeaking(false)
        break
      default:
        break
    }
  }, [appendToStreaming, feedSmoother, flushSmoother, stopSmoother])

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
    if (resumeId) {
      getSessionMessages(resumeId)
        .then((res) => {
          if (cancelled || !res?.messages?.length) return
          setMessages(res.messages.map((m) => ({
            id: nextId(),
            role: m.role,
            text: m.text || '',
            events: [],
            audioUrl: m.audio_url ? sessionAudioUrl(resumeId, m.seq) : null,
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
          // surface whatever prose was buffered before the drop.
          flushSmoother()
          stopSmoother()
          streamingId.current = null
          setBusy(false)
          setPendingAsk(null)
        },
      },
      { origin, resumeId },
    )
    clientRef.current = client
    client.connect()
    return () => { cancelled = true; client.disconnect(); stopSmoother(); clientRef.current = null }
  }, [origin, resumeId, onMessage, flushSmoother, stopSmoother])

  const send = useCallback((text) => {
    const t = (text || '').trim()
    if (!t || busy) return
    const ok = clientRef.current?.sendText(t)
    if (!ok) {
      setMessages((cur) => [...cur, { id: nextId(), role: 'assistant', error: true, events: [],
        text: '⚠ not connected to the daemon — is it running? (restart it after updates)' }])
      return
    }
    setMessages((cur) => [...cur, { id: nextId(), role: 'user', text: t, events: [] }])
    setBusy(true)
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
    const mic = new MicCapture({ onFrame: (int16) => clientRef.current?.sendAudio(int16) })
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
  useEffect(() => () => { micRef.current?.stop(); micRef.current = null }, [])

  // Replay a turn's spoken reply. In-session turns retain raw PCM chunks; a
  // resumed turn instead carries an audio_url to the persisted WAV (Phase 6b).
  const replay = useCallback(async (message) => {
    try { await audioPlayer.ensureContext() } catch { /* ignore */ }
    const chunks = message?.audioChunks
    if (chunks && chunks.length > 0) {
      audioPlayer.stop()
      const total = chunks.reduce((n, c) => n + c.length, 0)
      const merged = new Int16Array(total)
      let off = 0
      for (const c of chunks) { merged.set(c, off); off += c.length }
      audioPlayer.enqueuePcm(merged, message.audioSampleRate || 22050)
      return
    }
    if (message?.audioUrl) {
      audioPlayer.stop()
      try { await audioPlayer.playUrl(message.audioUrl) } catch { /* ignore */ }
    }
  }, [])

  const interrupt = useCallback(() => {
    clientRef.current?.interrupt()
    audioPlayer.stop()
    flushSmoother()
    stopSmoother()
    // Fallback: if the server doesn't ack within a moment (e.g. socket wedged),
    // release the UI so the user isn't stuck on "working".
    setTimeout(() => {
      if (streamingId.current) appendToStreaming((m) => ({ ...m, streaming: false }))
      streamingId.current = null
      setBusy(false)
    }, 1500)
  }, [appendToStreaming, flushSmoother, stopSmoother])

  return {
    messages, connected, busy, pendingAsk, hello,
    listening, speaking,
    send, answerAsk, interrupt, startVoice, stopVoice, replay,
  }
}
