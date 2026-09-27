import { useCallback, useEffect, useRef, useState } from 'react'

// How close to the bottom still counts as "following the output".
const NEAR_BOTTOM_PX = 80

// Follow the output only when the user is already at the bottom. This used to
// pin scrollTop unconditionally, and `messages` gets a new identity on every
// smoother tick (~60 Hz), so scrolling up during a reply was impossible — the
// next frame yanked it back. `deps` are what grows the thread.
export function useFollowOutput(deps) {
  const scrollRef = useRef(null)
  const [atBottom, setAtBottom] = useState(true)
  const onScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const slack = el.scrollHeight - el.scrollTop - el.clientHeight
    setAtBottom(slack <= NEAR_BOTTOM_PX)
  }, [])

  useEffect(() => {
    const el = scrollRef.current
    if (el && atBottom) el.scrollTop = el.scrollHeight
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, atBottom])

  const jumpToLatest = useCallback(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
    setAtBottom(true)
  }, [])

  return { scrollRef, atBottom, onScroll, jumpToLatest }
}
