import { useState, useEffect } from 'react'

export function useSettings() {
  const [settings, setSettings] = useState({})
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    ;(window.electronAPI?.getSettings() || Promise.resolve({})).then(s => {
      setSettings(s || {})
      setLoading(false)
    })
  }, [])

  async function save(updates) {
    await window.electronAPI?.saveSettings(updates)
    setSettings(prev => ({ ...prev, ...updates }))
  }

  return { settings, loading, save }
}
