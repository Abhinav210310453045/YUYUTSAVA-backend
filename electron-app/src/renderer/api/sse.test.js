// The daemon's event stream (R-ERR): each frame reaches its handler, a
// malformed frame is dropped with a warning, and a handler's own exception is
// not swallowed with the frame.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SSEClient } from './sse'

let es
beforeEach(() => {
  vi.stubGlobal(
    'EventSource',
    class {
      constructor(url) {
        this.url = url
        this.listeners = {}
        es = this
      }
      addEventListener(name, fn) {
        this.listeners[name] = fn
      }
      close() {}
    },
  )
})
afterEach(() => vi.unstubAllGlobals())

const frame = (name, data) => es.listeners[name]({ data })

describe('the stream', () => {
  it('sends each frame to its handler, parsed', () => {
    const got = []
    const on = (tag) => (d) => got.push([tag, d])
    new SSEClient({
      onEvent: on('event'),
      onAsk: on('ask'),
      onAskResolved: on('ask_resolved'),
      onSettings: on('settings'),
      onModels: on('models'),
      onConnected: () => got.push(['hello']),
    }).connect()
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
    new SSEClient({ onAsk }).connect()
    frame('ask', 'not json')
    expect(onAsk).not.toHaveBeenCalled()
    expect(warn.mock.calls[0][0]).toContain("'ask'")
    warn.mockRestore()
  })

  it("lets a handler's exception through", () => {
    new SSEClient({
      onProposal: () => {
        throw new Error('handler bug')
      },
    }).connect()
    expect(() => frame('proposal', '{}')).toThrow('handler bug')
  })
})
