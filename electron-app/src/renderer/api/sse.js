import { getBase } from './client'

// The daemon's /stream frames, by event name, and the handler each goes to.
const FRAMES = {
  event: 'onEvent',
  proposal: 'onProposal',
  ask: 'onAsk',
  // Resolution: an ask/proposal was answered (here or on the CLI) or expired —
  // remove the corresponding card so surfaces stay in sync.
  ask_resolved: 'onAskResolved',
  proposal_resolved: 'onProposalResolved',
  // Wake word fired on the daemon — surface to the voice overlay/panel.
  wake: 'onWake',
  // A runtime toggle changed (voice mode, dedicated subagents). Broadcast so
  // every surface — this window, the overlay, mobile — agrees without polling.
  settings: 'onSettings',
  // models.toml changed — in this window, another, the CLI or by hand. Carries
  // the whole GET /models status, new revision included.
  models: 'onModels',
}

export class SSEClient {
  constructor(handlers) {
    this.handlers = handlers
    this._es = null
    this._retryDelay = 1000
    this._stopped = false
  }

  connect() {
    if (this._es) return
    this._stopped = false
    this._open()
  }

  disconnect() {
    this._stopped = true
    if (this._es) {
      this._es.close()
      this._es = null
    }
  }

  _open() {
    const url = `${getBase()}/stream`
    this._es = new EventSource(url)

    this._es.addEventListener('hello', () => {
      this._retryDelay = 1000
      this.handlers.onConnected?.()
    })

    for (const [name, handler] of Object.entries(FRAMES)) {
      this._es.addEventListener(name, (e) => this._dispatch(name, handler, e.data))
    }

    this._es.onerror = () => {
      this._es?.close()
      this._es = null
      this.handlers.onDisconnected?.()
      if (!this._stopped) this._scheduleReconnect()
    }
  }

  // A frame that isn't JSON is dropped with a warning naming it. A handler's
  // own exception is not caught here: it surfaces as an uncaught error rather
  // than disappearing with the frame.
  _dispatch(name, handler, raw) {
    let data
    try {
      data = JSON.parse(raw)
    } catch (err) {
      console.warn(`sse: dropped a malformed '${name}' frame`, err)
      return
    }
    this.handlers[handler]?.(data)
  }

  _scheduleReconnect() {
    setTimeout(() => {
      if (!this._stopped) this._open()
    }, this._retryDelay)
    this._retryDelay = Math.min(this._retryDelay * 2, 10000)
  }
}
