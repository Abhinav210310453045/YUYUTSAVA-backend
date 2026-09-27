// The one drag-to-resize implementation: moves become sizes along the right
// axis, the body cursor is set for the drag and cleared after it, the window
// listeners are removed on release, and onEnd gets the last size.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { clamp, startResize } from './resize'

let listeners
beforeEach(() => {
  listeners = {}
  vi.stubGlobal('window', {
    addEventListener: (t, f) => (listeners[t] = f),
    removeEventListener: (t, f) => {
      if (listeners[t] === f) delete listeners[t]
    },
  })
  vi.stubGlobal('document', { body: { style: { cursor: '', userSelect: '' } } })
})
afterEach(() => vi.unstubAllGlobals())

const press = (x, y) => ({ clientX: x, clientY: y, preventDefault: vi.fn() })

describe('a horizontal drag', () => {
  it('turns moves into sizes and reports the last one on release', () => {
    const sizes = []
    const onEnd = vi.fn()
    const onStart = vi.fn()
    const e = press(100, 50)
    startResize(e, {
      sizeAt: (dx) => clamp(300 - dx, 180, 600),
      onSize: (s) => sizes.push(s),
      onStart,
      onEnd,
    })
    expect(e.preventDefault).toHaveBeenCalled()
    expect(onStart).toHaveBeenCalled()
    expect(document.body.style).toEqual({ cursor: 'col-resize', userSelect: 'none' })

    listeners.mousemove({ clientX: 80, clientY: 999 }) // y is ignored
    listeners.mousemove({ clientX: -900, clientY: 0 }) // past the max
    listeners.mouseup({})

    expect(sizes).toEqual([320, 600])
    expect(onEnd).toHaveBeenCalledWith(600)
    expect(document.body.style).toEqual({ cursor: '', userSelect: '' })
    expect(listeners).toEqual({})
  })

  it('reports the starting size when the pointer never moved', () => {
    const onEnd = vi.fn()
    startResize(press(0, 0), { sizeAt: (dx) => 320 + dx, onSize: () => {}, onEnd })
    listeners.mouseup({})
    expect(onEnd).toHaveBeenCalledWith(320)
  })
})

describe('a vertical drag', () => {
  it('follows y and shows the row cursor', () => {
    const sizes = []
    startResize(press(999, 500), {
      axis: 'y',
      sizeAt: (dy) => 200 - dy,
      onSize: (s) => sizes.push(s),
    })
    expect(document.body.style.cursor).toBe('row-resize')
    listeners.mousemove({ clientX: 0, clientY: 440 })
    listeners.mouseup({})
    expect(sizes).toEqual([260])
    expect(document.body.style.cursor).toBe('')
  })
})
