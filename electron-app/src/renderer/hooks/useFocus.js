import { useEffect, useState } from 'react'

// Tracks whether the window currently has OS-level focus. Reactive — re-renders
// on every focus/blur. Used by useNotifications to decide between in-window
// toasts and OS banners.
export function useFocus() {
  const [focused, setFocused] = useState(() => document.hasFocus())

  useEffect(() => {
    const onFocus = () => setFocused(true)
    const onBlur = () => setFocused(false)
    window.addEventListener('focus', onFocus)
    window.addEventListener('blur', onBlur)
    // visibilitychange covers minimize/hide on some platforms
    const onVis = () => setFocused(document.hasFocus())
    document.addEventListener('visibilitychange', onVis)
    return () => {
      window.removeEventListener('focus', onFocus)
      window.removeEventListener('blur', onBlur)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [])

  return focused
}
