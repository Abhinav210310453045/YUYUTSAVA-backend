import { useCallback, useEffect, useRef, useState } from 'react'
import { ConverseClient } from '../api/converse'
import { MicCapture } from '../audio/capture'

// STT dictation over the daemon's transcribe-only WS mode
// (/ws/converse?mode=dictate). Reuses the exact voice-chat capture path — the
// same MicCapture streams the same {type:"audio"} frames over a ConverseClient —
// but the server only runs VAD → STT: `transcript` frames come back as the
// user pauses and the caller decides where the text lands (the note editor
// inserts it — never auto-submits).
//
// One dictation = one connection: start() dials + opens the mic, stop()
// releases the mic, flushes the tail (audio_end) and disconnects once the
// server acks with dictate_done — bounded by a timeout so a wedged socket
// can't pin the UI in "finishing" forever.
const FINISH_TIMEOUT_MS = 10000

export function useDictation({ onText, onError } = {}) {
  const [dictating, setDictating] = useState(false) // mic hot, streaming
  const [finishing, setFinishing] = useState(false) // tail transcription draining
  const clientRef = useRef(null)
  const micRef = useRef(null)
  const timerRef = useRef(null)
  // Latest callbacks without re-creating start/stop (the WS handlers close
  // over these refs, not the props).
  const onTextRef = useRef(onText)
  const onErrorRef = useRef(onError)
  onTextRef.current = onText
  onErrorRef.current = onError

  const teardown = useCallback(() => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    const mic = micRef.current
    micRef.current = null
    if (mic) mic.stop()
    const client = clientRef.current
    clientRef.current = null
    if (client) client.disconnect()
    setDictating(false)
    setFinishing(false)
  }, [])

  const start = useCallback(async () => {
    if (clientRef.current) return
    const client = new ConverseClient(
      {
        onMessage: (msg) => {
          if (msg.type === 'transcript' && msg.text) onTextRef.current?.(msg.text)
          else if (msg.type === 'dictate_done') teardown()
          else if (msg.type === 'error') { onErrorRef.current?.(new Error(msg.message || 'dictation failed')); teardown() }
        },
        // A dropped socket can never deliver more transcripts — release the UI.
        // teardown() is idempotent and marks the client stopped, so this also
        // suppresses ConverseClient's auto-reconnect (a reconnect would greet a
        // fresh dictation loop with a dead mic).
        onDisconnected: () => { if (clientRef.current) teardown() },
      },
      { origin: 'dictate', mode: 'dictate' },
    )
    clientRef.current = client
    client.connect()
    const mic = new MicCapture({ onFrame: (int16) => clientRef.current?.sendAudio(int16) })
    micRef.current = mic
    try {
      await mic.start()
      setDictating(true)
    } catch (e) {
      teardown()
      onErrorRef.current?.(e)
    }
  }, [teardown])

  const stop = useCallback(async () => {
    const mic = micRef.current
    micRef.current = null
    setDictating(false)
    if (mic) await mic.stop()
    const client = clientRef.current
    if (!client) return
    setFinishing(true)
    client.endAudio()
    timerRef.current = setTimeout(teardown, FINISH_TIMEOUT_MS)
  }, [teardown])

  const toggle = useCallback(() => {
    if (dictating) stop()
    else if (!finishing) start()
  }, [dictating, finishing, start, stop])

  // Never leave a hot mic or a dangling socket behind on unmount.
  useEffect(() => () => { teardown() }, [teardown])

  return { dictating, finishing, start, stop, toggle }
}
