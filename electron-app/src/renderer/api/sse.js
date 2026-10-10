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

// Every SSEClient in this window shares one EventSource. Chromium keeps at most
// six HTTP/1.1 connections to a host; a stream per reader held them all open,
// and every other request to the daemon queued behind them, never answered.
const stream = { es: null, clients: new Set(), connected: false, retryDelay: 1000, timer: null }

function open() {
  const es = new EventSource(`${getBase()}/stream`)
  stream.es = es

  es.addEventListener('hello', () => {
    stream.retryDelay = 1000
    stream.connected = true
    deliver((c) => c.handlers.onConnected?.())
  })

  for (const [name, handler] of Object.entries(FRAMES)) {
    es.addEventListener(name, (e) => dispatch(name, handler, e.data))
  }

  es.onerror = () => {
    es.close()
    stream.es = null
    stream.connected = false
    deliver((c) => c.handlers.onDisconnected?.())
    if (stream.clients.size) scheduleReconnect()
  }
}

function close() {
  clearTimeout(stream.timer)
  stream.timer = null
  stream.es?.close()
  stream.es = null
  stream.connected = false
  stream.retryDelay = 1000
}

// Each client gets the frame even when another's handler throws; the first
// exception is then rethrown, so a handler's bug surfaces rather than
// disappearing with the frame.
function deliver(call) {
  let failure = null
  for (const client of [...stream.clients]) {
    try {
      call(client)
    } catch (err) {
      failure ??= err
    }
  }
  if (failure) throw failure
}

// A frame that isn't JSON is dropped with a warning naming it.
function dispatch(name, handler, raw) {
  let data
  try {
    data = JSON.parse(raw)
  } catch (err) {
    console.warn(`sse: dropped a malformed '${name}' frame`, err)
    return
  }
  deliver((c) => c.handlers[handler]?.(data))
}

function scheduleReconnect() {
  stream.timer = setTimeout(() => {
    stream.timer = null
    if (stream.clients.size && !stream.es) open()
  }, stream.retryDelay)
  stream.retryDelay = Math.min(stream.retryDelay * 2, 10000)
}

export class SSEClient {
  constructor(handlers) {
    this.handlers = handlers
  }

  connect() {
    if (stream.clients.has(this)) return
    stream.clients.add(this)
    if (stream.connected) {
      // The stream said hello before this client joined: greet it on its own.
      queueMicrotask(() => stream.clients.has(this) && this.handlers.onConnected?.())
    } else if (!stream.es && !stream.timer) {
      open()
    }
  }

  disconnect() {
    stream.clients.delete(this)
    if (!stream.clients.size) close()
  }
}
