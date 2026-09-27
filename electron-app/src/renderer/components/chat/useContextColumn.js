import { useCallback, useState } from 'react'
import { useViewState } from '../../nav/useViewState'
import { clamp, startResize } from '../../lib/resize'

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
    (e) =>
      startResize(e, {
        sizeAt: (dx) => clamp(ctxW - dx, CTX_MIN_W, CTX_MAX_W),
        onSize: setCtxW,
        onStart: () => setCtxDragging(true),
        onEnd: () => setCtxDragging(false),
      }),
    [ctxW, setCtxW],
  )
  return { ctxOpen, setCtxOpen, ctxW, ctxDragging, startCtxDrag }
}
