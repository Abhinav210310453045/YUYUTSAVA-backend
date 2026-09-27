// Drag a divider to resize a pane — the one implementation behind every
// ResizeHandle (activity rail, chat context column, board columns, tinker pane,
// attachments drawer).
//
// Call it from the handle's onMouseDown. Each pointer move becomes a size via
// `sizeAt(delta)` (delta = pixels moved along `axis` since the press) and goes
// to `onSize`. Until release the body shows the resize cursor and text can't be
// selected; on release that is undone and `onEnd` gets the last size (the
// starting size, `sizeAt(0)`, if the pointer never moved).
export function startResize(e, { axis = 'x', sizeAt, onSize, onStart, onEnd }) {
  e.preventDefault()
  const pos = (ev) => (axis === 'y' ? ev.clientY : ev.clientX)
  const origin = pos(e)
  let size = sizeAt(0)
  onStart?.()
  const onMove = (ev) => {
    size = sizeAt(pos(ev) - origin)
    onSize(size)
  }
  const onUp = () => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    onEnd?.(size)
  }
  document.body.style.cursor = axis === 'y' ? 'row-resize' : 'col-resize'
  document.body.style.userSelect = 'none'
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

export const clamp = (value, min, max) => Math.min(max, Math.max(min, value))
