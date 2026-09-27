import { useCallback, useState } from 'react'
import { clamp, startResize } from '../../../lib/resize'

// The board's one column width, dragged from a ResizeHandle and remembered
// app-wide in localStorage.

const COL_W_KEY = 'yy.todo.colW'
const COL_MIN = 240
const COL_MAX = 560

export function useColumnWidth() {
  const [colW, setColW] = useState(() => {
    const v = Number(localStorage.getItem(COL_W_KEY))
    return v >= COL_MIN && v <= COL_MAX ? v : 320
  })
  const [, setColDragging] = useState(false)

  const startColDrag = useCallback(
    (e) =>
      startResize(e, {
        sizeAt: (dx) => clamp(colW + dx, COL_MIN, COL_MAX),
        onSize: setColW,
        onStart: () => setColDragging(true),
        onEnd: (w) => {
          setColDragging(false)
          localStorage.setItem(COL_W_KEY, String(w))
        },
      }),
    [colW],
  )

  return { colW, startColDrag }
}
