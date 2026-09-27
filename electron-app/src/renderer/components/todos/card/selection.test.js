// A card's multi-select (R-UI-03): the toggle and its cap, the composer chips,
// and the reference block the tinker receives. What the tinker is told about
// the user's selection is decided here, so it is tested rather than eyeballed.

import { describe, expect, it } from 'vitest'

import { PHASE_ACCENT } from '../shared'
import { MAX_SELECTION, selectionChips, selectionContext, toggled, without } from './selection'

const card = {
  objectives: [
    { objective_id: 'o1', title: 'Map the surfaces', phase: 'thinking' },
    { objective_id: 'o2', title: 'Odd one', phase: 'no-such-phase' },
  ],
  notes: [
    { note_id: 'n1', author: 'tinker', body: 'line one\nline two' },
    { note_id: 'n2', author: 'user', body: 'x'.repeat(130) },
  ],
}

describe('the toggle', () => {
  it('adds, removes, and leaves the original set alone', () => {
    const a = new Set()
    const b = toggled(a, 'obj:o1')
    expect([...b]).toEqual(['obj:o1'])
    expect(a.size).toBe(0)
    expect([...toggled(b, 'obj:o1')]).toEqual([])
    expect([...without(b, 'obj:o1')]).toEqual([])
  })

  it('ignores a new key past the cap, but still lets one be removed', () => {
    const full = new Set(Array.from({ length: MAX_SELECTION }, (_, i) => `note:${i}`))
    expect(toggled(full, 'note:extra')).toBe(full)
    expect(toggled(full, 'note:0').size).toBe(MAX_SELECTION - 1)
  })
})

describe('the chips', () => {
  it('are null with nothing selected or no card', () => {
    expect(selectionChips(card, new Set())).toBe(null)
    expect(selectionChips(null, new Set(['obj:o1']))).toBe(null)
  })

  it('list objectives first, then notes, in card order, whatever the tick order', () => {
    const chips = selectionChips(card, new Set(['note:n1', 'obj:o2', 'obj:o1']))
    expect(chips.map((c) => c.key)).toEqual(['obj:o1', 'obj:o2', 'note:n1'])
    expect(chips[0]).toMatchObject({
      label: '◆ Map the surfaces',
      title: 'objective “Map the surfaces” [thinking]',
      accent: PHASE_ACCENT.thinking,
    })
    expect(chips[1].accent).toBe(PHASE_ACCENT.abandoned) // unknown phase
    expect(chips[2]).toMatchObject({ label: '✎ line one line two', title: 'note by tinker' })
  })

  it('cut a long note at 30 characters', () => {
    expect(selectionChips(card, new Set(['note:n2']))[0].label).toBe(`✎ ${'x'.repeat(30)}…`)
  })
})

describe('the reference block', () => {
  it('is empty with nothing selected', () => {
    expect(selectionContext(card, new Set())).toBe('')
  })

  it('names each item by its stable id, one line each, notes cut at 120', () => {
    expect(selectionContext(card, new Set(['obj:o1', 'note:n1', 'note:n2'])).split('\n')).toEqual([
      '[objective o1 "Map the surfaces" phase=thinking]',
      '[note n1 by tinker] "line one line two"',
      `[note n2 by user] "${'x'.repeat(120)}…"`,
    ])
  })
})
