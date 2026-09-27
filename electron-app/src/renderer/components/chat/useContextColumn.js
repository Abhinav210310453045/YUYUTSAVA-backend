import { useCallback, useState } from 'react'
import { useViewState } from '../../nav/useViewState'

// Context column bounds, matching the card view's tinker aside.
export const CTX_MIN_W = 200
export const CTX_MAX_W = 420
const CTX_DEFAULT_W = 260

// The chat's context/spend column: open or closed, its width, and a drag handle
// that resizes it. Open state and width survive a tab switch, per origin.
export function useContextColumn(origin) {
  // Context column: open/closed and width both survive a tab switch, the same
  // way the card view's tinker aside does.
  const [ctxOpen, setCtxOpen] = useViewState(`ctx:open:${origin}`, false)
  const [ctxW, setCtxW] = useViewState(`ctx:w:${origin}`, CTX_DEFAULT_W)
  const [ctxDragging, setCtxDragging] = useState(false)
  const startCtxDrag = useCallback(
    (e) => {
      e.preventDefault()
      const startX = e.clientX
      const startW = ctxW
      setCtxDragging(true)
      const onMove = (ev) => {
        setCtxW(Math.min(CTX_MAX_W, Math.max(CTX_MIN_W, startW - (ev.clientX - startX))))
      }
      const onUp = () => {
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', onUp)
        document.body.style.cursor = ''
        document.body.style.userSelect = ''
        setCtxDragging(false)
      }
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
    },
    [ctxW, setCtxW],
  )
  return { ctxOpen, setCtxOpen, ctxW, ctxDragging, startCtxDrag }
}
