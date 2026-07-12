import { getBase } from './client'
import { int16ToBase64 } from '../audio'

// WebSocket client for the daemon's interactive conversation channel
// (WS /ws/converse). Mirrors SSEClient's connect/disconnect + auto-reconnect
// shape. Frames are JSON; see daemon/web/routers/converse.py for the protocol.
export class ConverseClient {
  // `agent`/`card` select a server-side agent bundle: agent='tinker' with a
  // card id pins the conversation to that TODO card's thread (todo:<card_id>).
  // Omitted → the shared master deepagent, exactly as before.
  constructor(handlers, { origin = 'cli', resumeId = null, agent = null, card = null } = {}) {
    this.handlers = handlers
    this.origin = origin
    this.resumeId = resumeId
    this.agent = agent
    this.card = card
    this._ws = null
    this._retryDelay = 1000
    this._stopped = false
  }

  connect() {
    if (this._ws) return
    this._stopped = false
    this._open()
  }

  disconnect() {
    this._stopped = true
    if (this._ws) { try { this._ws.close() } catch {} this._ws = null }
  }

  // Pin the live thread so any future reconnect (socket drop, daemon restart)
  // RESUMES this conversation instead of minting a fresh one — otherwise the
  // history is lost on every reconnect. Called with the thread_id from `hello`.
  setResumeId(id) {
    if (id) this.resumeId = id
  }

  _wsUrl() {
    const base = getBase().replace(/^http/, 'ws')
    const qs = new URLSearchParams({ origin: this.origin })
    if (this.resumeId) qs.set('resume_id', this.resumeId)
    if (this.agent) qs.set('agent', this.agent)
    if (this.card) qs.set('card', this.card)
    return `${base}/ws/converse?${qs}`
  }

  _open() {
    let ws
    try {
      ws = new WebSocket(this._wsUrl())
    } catch {
      this._scheduleReconnect()
      return
    }
    this._ws = ws

    ws.onopen = () => { this._retryDelay = 1000; this.handlers.onConnected?.() }
    ws.onmessage = (e) => {
      let msg
      try { msg = JSON.parse(e.data) } catch { return }
      this.handlers.onMessage?.(msg)
    }
    ws.onclose = () => {
      this._ws = null
      this.handlers.onDisconnected?.()
      if (!this._stopped) this._scheduleReconnect()
    }
    ws.onerror = () => { try { ws.close() } catch {} }
  }

  _scheduleReconnect() {
    setTimeout(() => { if (!this._stopped) this._open() }, this._retryDelay)
    this._retryDelay = Math.min(this._retryDelay * 2, 10000)
  }

  _send(obj) {
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._ws.send(JSON.stringify(obj))
      return true
    }
    return false
  }

  sendText(text) { return this._send({ type: 'user_text', text }) }
  answerAsk(text) { return this._send({ type: 'ask_response', text }) }
  interrupt() { return this._send({ type: 'interrupt' }) }
  // Stream one frame of mic PCM (Int16Array, 16 kHz mono) to the daemon.
  sendAudio(int16) { return this._send({ type: 'audio', pcm: int16ToBase64(int16) }) }
  endAudio() { return this._send({ type: 'audio_end' }) }
}
