// The daemon's event stream (R-ERR): each frame reaches its handler, a
// malformed frame is dropped with a warning, and a handler's own exception is
// not swallowed with the frame. Every client in the window shares one
// connection, so readers can't use up the browser's six per host.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SSEClient } from './sse'

let es
let opened
let clients
beforeEach(() => {
  opened = 0
  clients = []
  vi.stubGlobal(
    'EventSource',
    class {
      constructor(url) {
        this.url = url
        this.listeners = {}
        this.closed = false
        es = this
        opened += 1
      }
      addEventListener(name, fn) {
        this.listeners[name] = fn
      }
      close() {
        this.closed = true
      }
    },
  )
})
afterEach(() => {
  for (const c of clients) c.disconnect()
  vi.unstubAllGlobals()
})

const client = (handlers) => {
  const c = new SSEClient(handlers)
  clients.push(c)
  c.connect()
  return c
}

const frame = (name, data) => es.listeners[name]({ data })

describe('the stream', () => {
  it('sends each frame to its handler, parsed', () => {
    const got = []
    const on = (tag) => (d) => got.push([tag, d])
    client({
      onEvent: on('event'),
      onAsk: on('ask'),
      onAskResolved: on('ask_resolved'),
      onSettings: on('settings'),
      onModels: on('models'),
      onConnected: () => got.push(['hello']),
    })
    expect(es.url).toMatch(/\/stream$/)
    es.listeners.hello()
    frame('event', '{"kind":"x"}')
    frame('ask', '{"ask_id":"a"}')
    frame('ask_resolved', '{"ask_id":"a"}')
    frame('settings', '{"voice":{}}')
    frame('models', '{"models":{"configured":true}}')
    frame('wake', '{}') // no handler registered: ignored
    expect(got).toEqual([
      ['hello'],
      ['event', { kind: 'x' }],
      ['ask', { ask_id: 'a' }],
      ['ask_resolved', { ask_id: 'a' }],
      ['settings', { voice: {} }],
      ['models', { models: { configured: true } }],
    ])
  })

  it('drops a malformed frame with a warning that names it', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const onAsk = vi.fn()
    client({ onAsk })
    frame('ask', 'not json')
    expect(onAsk).not.toHaveBeenCalled()
    expect(warn.mock.calls[0][0]).toContain("'ask'")
    warn.mockRestore()
  })

  it("lets a handler's exception through", () => {
    client({
      onProposal: () => {
        throw new Error('handler bug')
      },
    })
    expect(() => frame('proposal', '{}')).toThrow('handler bug')
  })

  it('gives every client one shared connection, closed with the last', () => {
    const a = vi.fn()
    const b = vi.fn()
    const first = client({ onModels: a })
    const second = client({ onModels: b })
    expect(opened).toBe(1)
    frame('models', '{"n":1}')
    expect(a).toHaveBeenCalledWith({ n: 1 })
    expect(b).toHaveBeenCalledWith({ n: 1 })
    first.disconnect()
    expect(es.closed).toBe(false)
    second.disconnect()
    expect(es.closed).toBe(true)
  })

  it('greets a client that joins after the stream said hello', async () => {
    client({})
    es.listeners.hello()
    const onConnected = vi.fn()
    client({ onConnected })
    await Promise.resolve()
    expect(onConnected).toHaveBeenCalledOnce()
  })

  it('still delivers to the others when one handler throws', () => {
    const after = vi.fn()
    client({
      onEvent: () => {
        throw new Error('handler bug')
      },
    })
    client({ onEvent: after })
    expect(() => frame('event', '{}')).toThrow('handler bug')
    expect(after).toHaveBeenCalledOnce()
  })
})
