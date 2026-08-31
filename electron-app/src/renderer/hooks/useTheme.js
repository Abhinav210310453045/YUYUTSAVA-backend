import { useCallback, useEffect, useState } from 'react'

// Named color themes. Each id maps to a `:root[data-theme="<id>"]` token block
// in styles/theme.css — adding a theme means adding a CSS block + a row here.
// Persisted in localStorage (mirrors the logs toggle in useSSE.jsx). Default
// is the neon-glass dark vibe ('dark' has no data-theme overrides).
export const THEMES = [
  { id: 'dark', label: 'Terminal' },
  { id: 'light', label: 'Daylight' },
  { id: 'meadow', label: 'Meadow' },
  { id: 'lagoon', label: 'Lagoon' },
  { id: 'sunset', label: 'Sunset' },
  { id: 'berry', label: 'Berry' },
]

const KEY = 'ui_theme'
const VALID = new Set(THEMES.map((t) => t.id))

function read() {
  try {
    const v = localStorage.getItem(KEY)
    return VALID.has(v) ? v : 'dark'
  } catch { return 'dark' }
}

function apply(theme) {
  document.documentElement.dataset.theme = theme
}

// Apply at module load, before the first React paint, so themed profiles
// don't flash dark on startup.
apply(read())

export function useTheme() {
  const [theme, setThemeState] = useState(read)

  useEffect(() => { apply(theme) }, [theme])

  const setTheme = useCallback((next) => {
    if (!VALID.has(next)) return
    try { localStorage.setItem(KEY, next) } catch { /* ignore */ }
    setThemeState(next)
  }, [])

  return { theme, setTheme, themes: THEMES }
}
