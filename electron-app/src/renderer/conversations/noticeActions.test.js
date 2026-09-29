// A notice's actions (R-UI-11): which are offered, and what a typed command means.

import { describe, expect, it } from 'vitest'

import { commandAction, liveActions } from './noticeActions'

const RETRY = { id: 'retry', label: 'Retry' }
const NEW = { id: 'new_chat', label: 'New chat' }
const notice = (actions) => ({ id: 'n', role: 'notice', text: 'stuck', actions })
const user = { id: 'u', role: 'user', text: 'hi' }

describe('typed commands', () => {
  it('"/new" and "/retry" are actions, anything else is a message', () => {
    expect(commandAction('/new')).toBe('new_chat')
    expect(commandAction('  /RETRY ')).toBe('retry')
    expect(commandAction('/new chat please')).toBe(null)
    expect(commandAction('new')).toBe(null)
    expect(commandAction('')).toBe(null)
  })
})

describe('live actions', () => {
  it('the newest notice offers its known actions', () => {
    expect(liveActions([user, notice([RETRY, NEW])], 1, false)).toEqual([RETRY, NEW])
  })

  it('unknown or malformed actions are not offered', () => {
    expect(liveActions([notice([{ id: 'dance' }, null, RETRY])], 0, false)).toEqual([RETRY])
  })

  it('nothing is offered once the conversation moved on, or while a turn runs', () => {
    expect(liveActions([notice([RETRY]), user], 0, false)).toEqual([])
    expect(liveActions([notice([RETRY])], 0, true)).toEqual([])
    expect(liveActions([user], 0, false)).toEqual([])
    expect(liveActions([notice(undefined)], 0, false)).toEqual([])
  })
})
