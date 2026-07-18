import React, { useEffect, useState } from 'react'

export default function WatchedDirsEditor() {
  const [roots, setRoots] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function refresh() {
    try {
      const cfg = await window.electronAPI?.getDaemonConfig('events')
      const fs = cfg?.sources?.fs
      setRoots(fs?.params?.roots || [])
      setError(null)
    } catch (e) {
      setError(e?.body?.message || e?.message || 'failed to load watched dirs')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { refresh() }, [])

  async function addDir() {
    setBusy(true)
    try {
      const path = await window.electronAPI?.pickDirectory()
      if (!path) return
      const res = await window.electronAPI?.addWatchedDir(path)
      setRoots(res?.roots || [])
      setError(null)
    } catch (e) {
      setError(e?.body?.message || e?.message || 'add failed')
    } finally {
      setBusy(false)
    }
  }

  async function removeDir(path) {
    setBusy(true)
    try {
      const res = await window.electronAPI?.removeWatchedDir(path)
      setRoots(res?.roots || [])
      setError(null)
    } catch (e) {
      setError(e?.body?.message || e?.message || 'remove failed')
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return <div style={{ color: 'var(--text-muted)', fontSize: 11, fontFamily: 'var(--font-mono)' }}>loading…</div>
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {error && (
        <div style={{
          background: 'rgba(255,51,102,0.06)',
          border: '1px solid rgba(255,51,102,0.25)',
          borderRadius: 'var(--radius-card)',
          padding: '6px 10px',
          fontSize: 11,
          color: 'var(--neon-red)',
          fontFamily: 'var(--font-mono)',
        }}>
          {error}
        </div>
      )}

      {roots.length === 0 && (
        <div style={{ color: 'var(--text-muted)', fontSize: 11, fontFamily: 'var(--font-mono)' }}>
          No watched directories. Add one below.
        </div>
      )}

      {roots.map(path => (
        <div key={path} style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '6px 10px',
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border-card)',
          borderRadius: 'var(--radius-card)',
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
        }}>
          <span style={{ flex: 1, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {path}
          </span>
          <button
            onClick={() => removeDir(path)}
            disabled={busy}
            style={{
              padding: '3px 10px',
              borderRadius: 'var(--radius-btn)',
              fontSize: 10,
              fontFamily: 'var(--font-mono)',
              fontWeight: 'var(--fw-semibold)',
              letterSpacing: '0.06em',
              textTransform: 'uppercase',
              border: '1px solid rgba(255,51,102,0.3)',
              background: 'rgba(255,51,102,0.06)',
              color: 'var(--neon-red)',
              cursor: busy ? 'not-allowed' : 'pointer',
              opacity: busy ? 0.5 : 1,
            }}
          >
            Remove
          </button>
        </div>
      ))}

      <button
        onClick={addDir}
        disabled={busy}
        style={{
          alignSelf: 'flex-start',
          padding: '6px 14px',
          marginTop: 4,
          borderRadius: 'var(--radius-btn)',
          fontSize: 11,
          fontFamily: 'var(--font-mono)',
          fontWeight: 'var(--fw-semibold)',
          letterSpacing: '0.06em',
          textTransform: 'uppercase',
          border: '1px solid rgba(var(--accent-rgb),0.3)',
          background: 'rgba(var(--accent-rgb),0.06)',
          color: 'var(--neon-green)',
          cursor: busy ? 'not-allowed' : 'pointer',
          opacity: busy ? 0.5 : 1,
        }}
      >
        + Add directory
      </button>
    </div>
  )
}
