import { useCallback, useState } from 'react'

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
    (e) => {
      e.preventDefault()
      const startX = e.clientX
      const startW = colW
      setColDragging(true)
      let w = startW
      const onMove = (ev) => {
        w = Math.min(COL_MAX, Math.max(COL_MIN, startW + (ev.clientX - startX)))
        setColW(w)
      }
      const onUp = () => {
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
        setColDragging(false)
        localStorage.setItem(COL_W_KEY, String(w))
      }
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
    },
    [colW],
  )

  return { colW, startColDrag }
}
