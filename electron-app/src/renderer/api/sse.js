import { getBase } from './client'

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
    if (this._es) { this._es.close(); this._es = null }
  }

  _open() {
    const url = `${getBase()}/stream`
    this._es = new EventSource(url)

    this._es.addEventListener('hello', () => {
      this._retryDelay = 1000
      this.handlers.onConnected?.()
    })

    this._es.addEventListener('event', (e) => {
      try { this.handlers.onEvent?.(JSON.parse(e.data)) } catch {}
    })

    this._es.addEventListener('proposal', (e) => {
      try { this.handlers.onProposal?.(JSON.parse(e.data)) } catch {}
    })

    this._es.addEventListener('ask', (e) => {
      try { this.handlers.onAsk?.(JSON.parse(e.data)) } catch {}
    })

    // Resolution events: an ask/proposal was answered (here or on the CLI) or
    // expired — remove the corresponding card so surfaces stay in sync.
    this._es.addEventListener('ask_resolved', (e) => {
      try { this.handlers.onAskResolved?.(JSON.parse(e.data)) } catch {}
    })

    this._es.addEventListener('proposal_resolved', (e) => {
      try { this.handlers.onProposalResolved?.(JSON.parse(e.data)) } catch {}
    })

    this._es.onerror = () => {
      this._es?.close()
      this._es = null
      this.handlers.onDisconnected?.()
      if (!this._stopped) this._scheduleReconnect()
    }
  }

  _scheduleReconnect() {
    setTimeout(() => {
      if (!this._stopped) this._open()
    }, this._retryDelay)
    this._retryDelay = Math.min(this._retryDelay * 2, 10000)
  }
}
