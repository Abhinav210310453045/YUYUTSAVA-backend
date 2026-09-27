// Characterization tests for the conversation store (R-UI-02): what every chat
// surface — chat panel, voice, TODO-card tinker chat — sees for each frame the
// daemon sends and each action the user takes. Written against the store
// before it was split, so the split has to keep them green.
//
// The socket, the history API, audio and the mic are replaced; the store's own
// logic (and the real TokenSmoother) runs.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({ clients: [], connected: true, history: { messages: [] } }))

vi.mock('../api/converse', () => ({
  ConverseClient: class {
    constructor(handlers, opts) {
      this.handlers = handlers
      this.opts = opts
      this.sent = []
      this.since = null
      this.resume = opts.resumeId
      h.clients.push(this)
    }
    connect() {}
    disconnect() {
      this.closed = true
    }
    setSinceSeq(s) {
      this.since = s
    }
    setResumeId(id) {
      this.resume = id
    }
    sendText(text, context) {
      this.sent.push(['text', text, context])
      return h.connected
    }
    answerAsk(text, id) {
      this.sent.push(['answer', text, id])
    }
    interrupt() {
      this.sent.push(['interrupt'])
    }
    sendAudio() {}
    endAudio() {}
  },
}))
vi.mock('../api/client', () => ({
  getBase: () => 'http://daemon',
  getSessionMessages: vi.fn(async () => h.history),
}))
vi.mock('../audio', () => ({
  audioPlayer: {
    isPlaying: () => false,
    isPaused: () => false,
    stop: vi.fn(),
    enqueuePcm: vi.fn(),
    onChange: () => () => {},
    ensureContext: async () => {},
  },
  base64ToInt16: () => new Int16Array([1, 2, 3]),
}))
vi.mock('../audio/capture', () => ({ MicCapture: class {} }))

const { acquireSession, _sessionsForTest } = await import('./sessions')
const { ConversationSession } = await import('./session')
const { isThreadVisible } = await import('./presence')

let keyN = 0
function session(opts = {}) {
  const s = acquireSession(`test-${++keyN}`, opts)
  s.retain()
  return { s, client: h.clients[h.clients.length - 1] }
}
const msgs = (s) => s.getSnapshot().messages
const frame = (s, msg) => s._onMessage(msg)

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('requestAnimationFrame', (cb) => setTimeout(() => cb(performance.now()), 16))
  vi.stubGlobal('cancelAnimationFrame', (id) => clearTimeout(id))
  h.connected = true
  h.history = { messages: [] }
})
afterEach(() => {
  for (const s of _sessionsForTest().values()) s.dispose()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('the handshake', () => {
  it('pins the thread it landed on and adopts the running state', () => {
    const { s, client } = session()
    frame(s, { type: 'hello', thread_id: 'th-1', session_id: 'se-1', run: { status: 'running' } })
    expect(s.resumeId).toBe('th-1')
    expect(client.resume).toBe('th-1')
    expect(s.getSnapshot().busy).toBe(true)
  })

  it('keeps a live usage reading rather than a null one', () => {
    const { s } = session()
    frame(s, { type: 'usage', tokens: 5 })
    frame(s, { type: 'hello', thread_id: 't', usage: null })
    expect(s.getSnapshot().usage).toEqual({ type: 'usage', tokens: 5 })
  })

  it('moves the resume cursor on sequenced frames, never on hello', () => {
    const { s, client } = session()
    frame(s, { type: 'token', text: 'a', seq: 7 })
    frame(s, { type: 'hello', thread_id: 't', seq: 99 })
    expect(s.lastSeq).toBe(7)
    expect(client.since).toBe(7)
  })

  it('moves the visible-thread marker to the thread it landed on', () => {
    const { s } = session({ resumeId: 'old' })
    s.setVisible(true)
    expect(isThreadVisible('old')).toBe(true)
    frame(s, { type: 'hello', thread_id: 'new' })
    expect(isThreadVisible('old')).toBe(false)
    expect(isThreadVisible('new')).toBe(true)
  })
})

describe('a turn', () => {
  it('splits prose around a tool call and hangs the result on the calling block', () => {
    const { s } = session()
    frame(s, { type: 'token', text: 'Looking' })
    frame(s, { type: 'tool_call', name: 'tr_ls', args: { path: '.' } })
    frame(s, { type: 'tool_result', name: 'tr_ls', preview: 'a.txt' })
    frame(s, { type: 'token', text: 'Found it' })
    frame(s, { type: 'turn_end' })
    const [first, second] = msgs(s)
    expect(first.text).toBe('Looking')
    expect(first.events.map((e) => e.kind)).toEqual(['tool_call', 'tool_result'])
    expect(first.streaming).toBe(false)
    expect(second.text).toBe('Found it')
    expect(second.streaming).toBe(false)
    expect(s.getSnapshot().busy).toBe(false)
  })

  it('attaches images and artifacts to the reply in flight', () => {
    const { s } = session()
    frame(s, { type: 'image', visual_id: 'v', url: '/visuals/v', kind: 'chart' })
    frame(s, { type: 'artifact', artifact_id: 'a', url: '/artifacts/a', kind: 'file' })
    const [m] = msgs(s)
    expect(m.images.map((i) => i.visual_id)).toEqual(['v'])
    expect(m.artifacts.map((a) => a.attachment_id)).toEqual(['a'])
  })

  it('uses final text only when nothing streamed', () => {
    const { s } = session()
    frame(s, { type: 'final', text: 'from the replay' })
    expect(msgs(s).map((m) => m.text)).toEqual(['from the replay'])

    const other = session().s
    frame(other, { type: 'token', text: 'streamed' })
    frame(other, { type: 'final', text: 'last block only' })
    expect(msgs(other).map((m) => m.text)).toEqual(['streamed'])
  })

  it('shows a log with no block to hang on, and every notice, as its own row', () => {
    const { s } = session()
    frame(s, { type: 'log', text: 'no assistant text' })
    frame(s, { type: 'notice', text: 'retrying', level: 'warning' })
    expect(msgs(s).map((m) => [m.role, m.text, m.level])).toEqual([
      ['notice', 'no assistant text', 'info'],
      ['notice', 'retrying', 'warning'],
    ])
  })

  it('an error ends the turn with a visible error bubble', () => {
    const { s } = session()
    frame(s, { type: 'turn_start', text: 'hi' })
    frame(s, { type: 'error', message: 'boom' })
    const last = msgs(s).at(-1)
    expect([last.text, last.error]).toEqual(['⚠ boom', true])
    expect(s.getSnapshot().busy).toBe(false)
  })

  it('a low-confidence transcript gets a clarify bubble', () => {
    const { s } = session()
    frame(s, { type: 'clarify', message: 'say again?' })
    expect(msgs(s).at(-1)).toMatchObject({ role: 'assistant', text: 'say again?', clarify: true })
  })

  it('a spoken reply is played and kept for replay', () => {
    const { s } = session()
    frame(s, { type: 'token', text: 'hi' })
    frame(s, { type: 'audio_chunk', pcm: 'AAA', sample_rate: 16000 })
    const m = msgs(s)[0]
    expect(m.audioChunks).toHaveLength(1)
    expect(m.audioSampleRate).toBe(16000)
  })
})

describe('the user side', () => {
  it('does not echo back a turn this view sent itself', () => {
    const { s, client } = session()
    expect(s.send('hello')).toBe(true)
    frame(s, { type: 'turn_start', text: 'hello' })
    expect(msgs(s).filter((m) => m.role === 'user')).toHaveLength(1)
    expect(client.sent).toEqual([['text', 'hello', null]])
  })

  it('renders a turn another view started, without its selection block', () => {
    const { s } = session()
    frame(s, {
      type: 'turn_start',
      text: '<selection-context>\n[card]\n</selection-context>\n\nwhat next?',
    })
    expect(msgs(s).map((m) => [m.role, m.text])).toEqual([['user', 'what next?']])
  })

  it('a spoken utterance becomes the user turn', () => {
    const { s } = session()
    frame(s, { type: 'transcript', text: 'turn on the lights' })
    expect(msgs(s).map((m) => m.text)).toEqual(['turn on the lights'])
    expect(s.getSnapshot().busy).toBe(true)
  })

  it('a message typed mid-turn is queued, then sent when the turn ends', () => {
    const { s, client } = session()
    s.send('first')
    expect(s.send('second')).toBe(true)
    expect(s.getSnapshot().queued).toEqual({ text: 'second', context: null })
    frame(s, { type: 'turn_end' })
    vi.runOnlyPendingTimers()
    expect(client.sent.map((x) => x[1])).toEqual(['first', 'second'])
    expect(s.getSnapshot().queued).toBe(null)
  })

  it('says so when the daemon is not connected', () => {
    h.connected = false
    const { s } = session()
    expect(s.send('hi')).toBe(false)
    expect(msgs(s).at(-1)).toMatchObject({ role: 'assistant', error: true })
  })

  it('an empty message is not sent', () => {
    const { s, client } = session()
    expect(s.send('   ')).toBe(false)
    expect(client.sent).toEqual([])
  })
})

describe('asks', () => {
  it('queues every question, answers by id, and clears them when the turn ends', () => {
    const { s, client } = session()
    frame(s, { type: 'ask', ask: { ask_id: 'a1', body: 'one?' } })
    frame(s, { type: 'ask', ask: { ask_id: 'a2', body: 'two?' } })
    expect(s.getSnapshot().pendingAsks.map((a) => a.ask_id)).toEqual(['a1', 'a2'])
    expect(s.getSnapshot().pendingAsk.ask_id).toBe('a1')

    s.answerAsk('yes', 'a2')
    expect(client.sent.at(-1)).toEqual(['answer', 'yes', 'a2'])
    expect(s.getSnapshot().pendingAsks.map((a) => a.ask_id)).toEqual(['a1'])

    frame(s, { type: 'ask_resolved', ask_id: 'a1' })
    expect(s.getSnapshot().pendingAsk).toBe(null)

    frame(s, { type: 'ask', ask: { ask_id: 'a3' } })
    frame(s, { type: 'turn_end' })
    expect(s.getSnapshot().pendingAsks).toEqual([])
  })

  it('a re-sent ask replaces its earlier copy instead of duplicating it', () => {
    const { s } = session()
    frame(s, { type: 'ask', ask: { ask_id: 'a1', body: 'v1' } })
    frame(s, { type: 'ask', ask: { ask_id: 'a1', body: 'v2' } })
    expect(s.getSnapshot().pendingAsks).toEqual([{ ask_id: 'a1', body: 'v2' }])
  })
})

describe('history and lifecycle', () => {
  it('hydrates a resumed thread from its history', async () => {
    h.history = {
      messages: [
        { role: 'user', text: '<selection-context>\nx\n</selection-context>\n\nhi' },
        { role: 'assistant', text: 'hello', audio_url: '/voice/1.wav', artifacts: [{ id: 1 }] },
      ],
    }
    const { s } = session({ resumeId: 'th-9' })
    await vi.runAllTimersAsync()
    expect(msgs(s).map((m) => [m.role, m.text])).toEqual([
      ['user', 'hi'],
      ['assistant', 'hello'],
    ])
    expect(msgs(s)[1].audioUrl).toBe('http://daemon/voice/1.wav')
    expect(msgs(s)[1].artifacts).toEqual([{ id: 1 }])
  })

  it('one session per key, until it is disposed', () => {
    const a = acquireSession('same-key')
    expect(acquireSession('same-key')).toBe(a)
    a.dispose()
    expect(acquireSession('same-key')).not.toBe(a)
  })

  it('an unwatched, idle session lets go after the idle window; a busy one holds on', () => {
    const idle = acquireSession('idle-key')
    const busy = acquireSession('busy-key')
    busy._onMessage({ type: 'turn_start', text: 'working' })
    vi.advanceTimersByTime(11 * 60 * 1000)
    expect(idle.disposed).toBe(true)
    expect(busy.disposed).toBe(false)
  })

  it('beyond the cap, the least recently touched idle sessions are evicted', () => {
    const first = acquireSession('evict-0')
    for (let i = 1; i <= 24; i += 1) acquireSession(`evict-${i}`)
    expect(first.disposed).toBe(true)
    expect(_sessionsForTest().size).toBeLessThanOrEqual(24)
  })

  it('a new session in place drops the thread and reconnects', () => {
    const { s, client } = session({ resumeId: 'th-1' })
    frame(s, { type: 'token', text: 'x' })
    s.newSession()
    expect(client.closed).toBe(true)
    expect(s.resumeId).toBe(null)
    expect(msgs(s)).toEqual([])
    expect(h.clients.at(-1)).not.toBe(client)
  })

  it('is a class every surface can construct', () => {
    expect(typeof ConversationSession).toBe('function')
  })
})
