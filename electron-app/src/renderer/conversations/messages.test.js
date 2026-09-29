// The message list as pure functions (R-UI-02): rows, patches, the turn-start
// dedupe, history, and the ask queue. No session, no socket.

import { describe, expect, it } from 'vitest'

import {
  alreadyShown,
  errorMessage,
  fromHistory,
  lastAssistantId,
  noticeMessage,
  patchMessage,
  stripSelection,
  streamingMessage,
  userMessage,
  withAsk,
  withoutAsk,
} from './messages'

const user = (text) => ({ id: `u-${text}`, role: 'user', text, events: [] })
const reply = (text, streaming = false) => ({ id: `a-${text}`, role: 'assistant', text, streaming })

describe('rows', () => {
  it('each has a fresh id and the shape its view expects', () => {
    const [a, b] = [userMessage('x'), userMessage('x')]
    expect(a.id).not.toBe(b.id)
    expect(errorMessage('⚠ no')).toMatchObject({ role: 'assistant', error: true, text: '⚠ no' })
    expect(noticeMessage('careful', 'warning')).toMatchObject({
      role: 'notice',
      level: 'warning',
      streaming: false,
    })
    expect(streamingMessage('s1')).toMatchObject({ id: 's1', text: '', streaming: true })
  })

  it('a notice keeps what the daemon said can be done about it', () => {
    const actions = [{ id: 'retry', label: 'Retry' }]
    expect(noticeMessage('stuck', 'error', { code: 'empty_turn', actions })).toMatchObject({
      code: 'empty_turn',
      actions,
    })
    expect(noticeMessage('fyi')).toMatchObject({ code: null, actions: [] })
    expect(noticeMessage('fyi', 'info', { actions: 'junk' }).actions).toEqual([])
  })

  it('the selection block is stripped, anything else kept', () => {
    expect(stripSelection('<selection-context>\n[a]\n</selection-context>\n\nwhy?')).toBe('why?')
    expect(stripSelection('plain')).toBe('plain')
    expect(stripSelection(null)).toBe('')
  })
})

describe('the list', () => {
  it('patches one message by id, leaving the rest as they were', () => {
    const list = [user('a'), reply('b')]
    const out = patchMessage(list, 'a-b', (m) => ({ ...m, text: 'B' }))
    expect(out.map((m) => m.text)).toEqual(['a', 'B'])
    expect(out[0]).toBe(list[0])
  })

  it('finds the newest assistant block', () => {
    expect(lastAssistantId([reply('1'), user('q'), reply('2'), user('q2')])).toBe('a-2')
    expect(lastAssistantId([user('q')])).toBe(null)
  })

  it('a turn is already shown when the newest user bubble matches and is unanswered', () => {
    expect(alreadyShown([user('hi')], 'hi')).toBe(true)
    expect(alreadyShown([user('hi'), reply('…', true)], 'hi')).toBe(true)
    expect(alreadyShown([user('hi'), reply('hello')], 'hi')).toBe(false) // answered: a repeat
    expect(alreadyShown([user('hi')], 'bye')).toBe(false)
    expect(alreadyShown([], 'hi')).toBe(false)
  })
})

describe('history', () => {
  it('maps the endpoint rows, stripping selections and resolving audio', () => {
    const rows = fromHistory(
      {
        messages: [
          { role: 'user', text: '<selection-context>\nx\n</selection-context>\n\nq' },
          { role: 'assistant', text: null, audio_url: '/voice/1.wav', artifacts: [1] },
        ],
      },
      'http://d',
    )
    expect(rows.map((r) => [r.role, r.text, r.audioUrl, r.artifacts])).toEqual([
      ['user', 'q', null, []],
      ['assistant', '', 'http://d/voice/1.wav', [1]],
    ])
  })

  it('no history is an empty list', () => {
    expect(fromHistory(null, 'http://d')).toEqual([])
  })
})

describe('the ask queue', () => {
  it('appends, replaces a re-sent ask in place of its old copy, and removes by id', () => {
    let q = withAsk([], { ask_id: 'a', v: 1 })
    q = withAsk(q, { ask_id: 'b' })
    q = withAsk(q, { ask_id: 'a', v: 2 })
    expect(q).toEqual([{ ask_id: 'b' }, { ask_id: 'a', v: 2 }])
    expect(withoutAsk(q, 'b')).toEqual([{ ask_id: 'a', v: 2 }])
  })

  it('no id clears the queue — the turn ended', () => {
    expect(withoutAsk([{ ask_id: 'a' }], null)).toEqual([])
  })
})
