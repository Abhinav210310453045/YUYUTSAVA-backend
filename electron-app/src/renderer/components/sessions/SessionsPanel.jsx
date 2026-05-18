import React, { useEffect, useState, useCallback } from 'react'
import { listSessions } from '../../api/client'
import SessionRow from './SessionRow'

const POLL_MS = 5000

export default function SessionsPanel() {
  const [sessions, setSessions] = useState([])
  const [error, setError] = useState(null)
  const [loaded, setLoaded] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const rows = await listSessions(null, 100)
      setSessions(rows)
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoaded(true)
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, POLL_MS)
    return () => clearInterval(t)
  }, [refresh])

  const onDeleted = useCallback((id) => {
    setSessions((cur) => cur.filter((s) => s.id !== id))
  }, [])

  const isEmpty = loaded && sessions.length === 0

  return (
    <div style={{
      flex: 1,
      overflowY: 'auto',
      padding: '20px 24px',
      display: 'flex',
      flexDirection: 'column',
      gap: 12,
    }}>
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        marginBottom: 4,
      }}>
        <h2 style={{
          fontSize: 13,
          fontWeight: 600,
          fontFamily: 'var(--font-mono)',
          color: 'var(--text-primary)',
          textTransform: 'uppercase',
          letterSpacing: '0.1em',
          margin: 0,
        }}>
          Sessions
        </h2>
        {sessions.length > 0 && (
          <span style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            color: 'var(--neon-green)',
            background: 'rgba(0,255,136,0.08)',
            border: '1px solid rgba(0,255,136,0.2)',
            borderRadius: 10,
            padding: '1px 7px',
          }}>
            {sessions.length}
          </span>
        )}
      </div>

      {error && (
        <div style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          color: 'var(--neon-red)',
          padding: '6px 10px',
          border: '1px solid rgba(255,51,102,0.25)',
          borderRadius: 6,
          background: 'rgba(255,51,102,0.05)',
        }}>
          {`> sessions endpoint: ${error}`}
        </div>
      )}

      {isEmpty && !error && (
        <div style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 12,
          color: 'var(--text-muted)',
          fontFamily: 'var(--font-mono)',
          fontSize: 12,
        }}>
          <div style={{ fontSize: 32, opacity: 0.3 }}>⏱</div>
          <div>{'> no sessions yet'}</div>
          <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
            run <span style={{ color: 'var(--neon-green)' }}>uv run yuyutsava &lt;task&gt;</span> in your terminal
          </div>
        </div>
      )}

      {sessions.map((s) => (
        <SessionRow key={s.id} session={s} onDeleted={onDeleted} />
      ))}
    </div>
  )
}
