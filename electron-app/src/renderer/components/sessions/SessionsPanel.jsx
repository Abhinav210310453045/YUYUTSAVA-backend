import React, { useEffect, useState, useCallback } from 'react'
import { listSessions } from '../../api/client'
import SessionRow from './SessionRow'

const POLL_MS = 5000

// Column definitions — the split is DB-backed via the session `origin` field.
const COLUMNS = [
  { key: 'cli', title: 'CLI', hint: 'terminal sessions', empty: 'uv run yuyutsava <task>' },
  { key: 'ui', title: 'UI Chats', hint: 'chats from this app', empty: 'start a chat in the Chat tab' },
  { key: 'voice', title: 'Voice', hint: 'voice conversations', empty: 'talk in the Voice tab' },
]

function ColumnHeader({ title, count }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
      <h2 style={{
        fontSize: 13,
        fontWeight: 'var(--fw-semibold)',
        fontFamily: 'var(--font-mono)',
        color: 'var(--text-primary)',
        textTransform: 'uppercase',
        letterSpacing: '0.1em',
        margin: 0,
      }}>
        {title}
      </h2>
      {count > 0 && (
        <span style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          color: 'var(--neon-green)',
          background: 'rgba(var(--accent-rgb),0.08)',
          border: '1px solid rgba(var(--accent-rgb),0.2)',
          borderRadius: 10,
          padding: '1px 7px',
        }}>
          {count}
        </span>
      )}
    </div>
  )
}

function SessionColumn({ col, sessions, loaded, error, onDeleted, onOpenSession }) {
  const isEmpty = loaded && sessions.length === 0
  return (
    <div style={{
      flex: 1,
      minWidth: 0,
      display: 'flex',
      flexDirection: 'column',
      gap: 12,
      overflow: 'hidden',
    }}>
      <ColumnHeader title={col.title} count={sessions.length} />

      <div style={{
        flex: 1,
        overflowY: 'auto',
        display: 'flex',
        flexDirection: 'column',
        gap: 12,
        paddingRight: 4,
        scrollbarWidth: 'thin',
      }}>
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
            padding: '24px 8px',
          }}>
            <div style={{ fontSize: 28, opacity: 0.3 }}>⏱</div>
            <div>{'> none yet'}</div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)', textAlign: 'center' }}>
              {col.empty}
            </div>
          </div>
        )}

        {sessions.map((s) => (
          <SessionRow key={s.id} session={s} onDeleted={onDeleted} onOpenSession={onOpenSession} />
        ))}
      </div>
    </div>
  )
}

export default function SessionsPanel({ onOpenSession }) {
  // One bucket per origin column.
  const [byOrigin, setByOrigin] = useState({ cli: [], ui: [], voice: [] })
  const [error, setError] = useState(null)
  const [loaded, setLoaded] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const results = await Promise.all(
        COLUMNS.map((c) => listSessions(null, 100, null, c.key)),
      )
      const next = {}
      // A session with zero messages has never had a real turn — it's either
      // a chat the user hasn't started yet or one about to be swept by
      // discard_if_unused (see ConversationService). Either way it's not
      // useful to resume and clicking into it can hit a dead session id, so
      // it's left out of the list rather than shown as a dead-end row.
      COLUMNS.forEach((c, i) => { next[c.key] = results[i].filter((s) => s.message_count > 0) })
      setByOrigin(next)
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
    setByOrigin((cur) => {
      const next = {}
      for (const k of Object.keys(cur)) next[k] = cur[k].filter((s) => s.id !== id)
      return next
    })
  }, [])

  return (
    <div style={{
      flex: 1,
      overflow: 'hidden',
      padding: '20px 24px',
      display: 'flex',
      gap: 24,
    }}>
      {COLUMNS.map((col) => (
        <SessionColumn
          key={col.key}
          col={col}
          sessions={byOrigin[col.key] || []}
          loaded={loaded}
          error={error}
          onDeleted={onDeleted}
          onOpenSession={onOpenSession}
        />
      ))}
    </div>
  )
}
