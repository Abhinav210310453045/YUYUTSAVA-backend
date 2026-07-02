import { useCallback, useEffect, useState } from 'react'

// Light/dark theme toggle. Sets `data-theme` on <html>; the token overrides in
// styles/theme.css do the rest. Persisted in localStorage (mirrors the logs
// toggle in useSSE.jsx). Default is the neon-glass dark vibe.
const KEY = 'ui_theme'

function read() {
  try { return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark' } catch { return 'dark' }
}

function apply(theme) {
  document.documentElement.dataset.theme = theme
}

export function useTheme() {
  const [theme, setTheme] = useState(read)

  useEffect(() => { apply(theme) }, [theme])

  const toggle = useCallback(() => {
    setTheme((cur) => {
      const next = cur === 'light' ? 'dark' : 'light'
      try { localStorage.setItem(KEY, next) } catch { /* ignore */ }
      return next
    })
  }, [])

  return { theme, toggle }
}
