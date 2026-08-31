import React, { useEffect, useState, useCallback } from 'react'
import { listTodos, createTodo, deleteTodo } from '../../api/client'
import TodoCardView from './TodoCardView'
import { STATUS_ACCENT, PHASE_ACCENT, TagChips, PinIcon, humanAge } from './shared'
import { useNav } from '../../nav/NavProvider'
import { dropViewState, useScrollRestore } from '../../nav/useViewState'

const POLL_MS = 5000

// One board column per card status (the four exchange CardStatus values).
const COLUMNS = [
  { key: 'inbox', title: 'Inbox', empty: 'capture an idea above' },
  { key: 'active', title: 'Active', empty: 'nothing in flight' },
  { key: 'done', title: 'Done', empty: 'nothing finished yet' },
  { key: 'archived', title: 'Archived', empty: 'nothing shelved' },
]

function CountBadge({ label, count }) {
  return (
    <span title={label} style={{ color: 'var(--text-muted)', fontSize: 11 }}>
      {label}: {count}
    </span>
  )
}

function TodoCard({ card, onOpen, onDeleted }) {
  const [deleting, setDeleting] = useState(false)
  const accent = STATUS_ACCENT[card.status] || STATUS_ACCENT.inbox

  const onDelete = async (e) => {
    e.stopPropagation()
    if (!confirm(`Delete TODO "${card.title}"?\n\nNotes and attachments go with it.`)) return
    setDeleting(true)
    try {
      await deleteTodo(card.card_id)
      onDeleted?.(card.card_id)
    } catch (err) {
      alert(`Delete failed: ${err.message}`)
      setDeleting(false)
    }
  }

  return (
    <div
      className="hover-bulge"
      onClick={() => onOpen?.(card.card_id)}
      title="click to open this card"
      style={{
        background: 'var(--bg-elevated, #1a1a2e)',
        border: `1px solid ${accent.border}`,
        borderLeft: `3px solid ${accent.bar}`,
        borderRadius: 8,
        boxShadow: `0 2px 10px rgba(0, 0, 0, 0.45), 0 0 16px ${accent.glow}`,
        '--bulge-glow': accent.hover,
        padding: '12px 14px',
        fontFamily: 'var(--font-mono)',
        fontSize: 12,
        color: 'var(--text-primary)',
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        cursor: 'pointer',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
        {card.pinned && (
          <span title="pinned" style={{ flexShrink: 0, marginTop: 1 }}>
            <PinIcon />
          </span>
        )}
        <span style={{
          flex: 1,
          fontWeight: 'var(--fw-semibold)',
          wordBreak: 'break-word',
          color: 'var(--text-primary)',
        }}>
          {card.title}
        </span>
        <button
          onClick={onDelete}
          disabled={deleting}
          title="delete card"
          style={{
            flexShrink: 0,
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            padding: '2px 7px',
            background: 'transparent',
            color: 'var(--neon-red)',
            border: '1px solid rgba(255,51,102,0.25)',
            borderRadius: 6,
            cursor: deleting ? 'default' : 'pointer',
            opacity: deleting ? 0.5 : 1,
          }}
        >
          {deleting ? '...' : '✕'}
        </button>
      </div>

      <TagChips tags={card.tags} />

      {/* Think-flow progress: completed / total objectives (summary counts). */}
      {card.objective_count > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{
            flex: 1, height: 4, borderRadius: 2,
            background: 'rgba(255,255,255,0.06)', overflow: 'hidden',
          }}>
            <div style={{
              width: `${Math.round((card.objective_done_count / card.objective_count) * 100)}%`,
              height: '100%', borderRadius: 2,
              background: PHASE_ACCENT.completed.bar,
              opacity: 0.8, transition: 'width 0.3s ease',
            }} />
          </div>
          <span style={{ color: 'var(--text-muted)', fontSize: 10, whiteSpace: 'nowrap' }}>
            {card.objective_done_count}/{card.objective_count}
          </span>
        </div>
      )}

      <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
        <CountBadge label="notes" count={card.note_count} />
        <span style={{ color: 'var(--text-dim)' }}>·</span>
        <CountBadge label="files" count={card.attachment_count} />
        <span style={{ flex: 1 }} />
        <span style={{ color: 'var(--text-dim)', fontSize: 10 }}>{humanAge(card.updated_ts)}</span>
      </div>
    </div>
  )
}

function BoardColumn({ col, cards, loaded, error, onOpen, onDeleted }) {
  // Come back to the column scrolled where you left it, not at the top.
  const scrollRef = useScrollRestore(loaded, `todos/col/${col.key}`)
  const accent = STATUS_ACCENT[col.key]
  const isEmpty = loaded && cards.length === 0
  return (
    <div style={{
      flex: 1,
      minWidth: 0,
      display: 'flex',
      flexDirection: 'column',
      gap: 12,
      overflow: 'hidden',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: accent.bar, flexShrink: 0 }} />
        <h2 style={{
          fontSize: 13,
          fontWeight: 'var(--fw-semibold)',
          fontFamily: 'var(--font-mono)',
          color: 'var(--text-primary)',
          textTransform: 'uppercase',
          letterSpacing: '0.1em',
          margin: 0,
        }}>
          {col.title}
        </h2>
        {cards.length > 0 && (
          <span style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            color: accent.bar,
            background: accent.glow,
            border: `1px solid ${accent.border}`,
            borderRadius: 10,
            padding: '1px 7px',
          }}>
            {cards.length}
          </span>
        )}
      </div>

      <div ref={scrollRef} style={{
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
            {`> todos endpoint: ${error}`}
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
            <div style={{ fontSize: 28, opacity: 0.3 }}>☐</div>
            <div>{'> none'}</div>
            <div style={{ fontSize: 11, color: 'var(--text-dim)', textAlign: 'center' }}>
              {col.empty}
            </div>
          </div>
        )}

        {cards.map((c) => (
          <TodoCard key={c.card_id} card={c} onOpen={onOpen} onDeleted={onDeleted} />
        ))}
      </div>
    </div>
  )
}

export default function TodosPanel() {
  const { params, push } = useNav()
  const [cards, setCards] = useState([])
  const [error, setError] = useState(null)
  const [loaded, setLoaded] = useState(false)
  // Which card is open is navigation state, not component state: it survives
  // leaving the tab, comes back with the back arrow, and is restored on reload.
  const openId = params.cardId || null
  const [newTitle, setNewTitle] = useState('')
  const [creating, setCreating] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setCards(await listTodos())
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoaded(true)
    }
  }, [])

  // Poll while the board is visible — agents write TODOs too (todo_add from
  // chat/CLI), so the board tracks their changes without a manual refresh.
  useEffect(() => {
    if (openId) return undefined
    refresh()
    const t = setInterval(refresh, POLL_MS)
    return () => clearInterval(t)
  }, [refresh, openId])

  const onCreate = async () => {
    const title = newTitle.trim()
    if (!title || creating) return
    setCreating(true)
    try {
      await createTodo(title)
      setNewTitle('')
      await refresh()
    } catch (e) {
      alert(`Create failed: ${e.message}`)
    } finally {
      setCreating(false)
    }
  }

  const onDeleted = useCallback((cardId) => {
    setCards((cur) => cur.filter((c) => c.card_id !== cardId))
    dropViewState(`todos/card/${cardId}`)
  }, [])

  const openCard = useCallback((cardId) => push({ cardId }), [push])

  if (openId) return <TodoCardView cardId={openId} />

  // Pinned cards float to the top of their column; ties keep the freshest first.
  const byStatus = {}
  for (const col of COLUMNS) byStatus[col.key] = []
  for (const c of cards) (byStatus[c.status] || byStatus.inbox).push(c)
  for (const key of Object.keys(byStatus)) {
    byStatus[key].sort((a, b) => (b.pinned - a.pinned) || (b.updated_ts - a.updated_ts))
  }

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '14px 24px', borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-bar)',
      }}>
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 11, letterSpacing: '0.1em',
          textTransform: 'uppercase', color: 'var(--text-primary)', fontWeight: 'var(--fw-semibold)',
        }}>Todos — board</span>
        <input
          value={newTitle}
          onChange={(e) => setNewTitle(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') onCreate() }}
          placeholder="new TODO title…"
          style={{
            marginLeft: 'auto', flex: 1, maxWidth: 420,
            background: 'var(--bg-card)', color: 'var(--text-primary)',
            border: '1px solid var(--border-card)', borderRadius: 6, padding: '5px 10px',
            fontFamily: 'var(--font-mono)', fontSize: 12, outline: 'none',
          }}
        />
        <button
          onClick={onCreate}
          disabled={creating || !newTitle.trim()}
          style={{
            fontFamily: 'var(--font-mono)', fontSize: 11, padding: '5px 12px',
            background: 'rgba(var(--accent-rgb),0.08)', color: 'var(--neon-green)',
            border: '1px solid rgba(var(--accent-rgb),0.25)', borderRadius: 6,
            cursor: creating || !newTitle.trim() ? 'default' : 'pointer',
            opacity: creating || !newTitle.trim() ? 0.5 : 1,
          }}
        >
          {creating ? '...' : '+ Add'}
        </button>
        <button
          onClick={refresh}
          title="Refresh"
          style={{
            fontFamily: 'var(--font-mono)', fontSize: 11, padding: '5px 10px',
            background: 'rgba(var(--accent-rgb),0.08)', color: 'var(--neon-green)',
            border: '1px solid rgba(var(--accent-rgb),0.25)', borderRadius: 6, cursor: 'pointer',
          }}
        >↻</button>
      </div>

      <div style={{
        flex: 1,
        overflow: 'hidden',
        padding: '20px 24px',
        display: 'flex',
        gap: 24,
      }}>
        {COLUMNS.map((col) => (
          <BoardColumn
            key={col.key}
            col={col}
            cards={byStatus[col.key]}
            loaded={loaded}
            error={error}
            onOpen={openCard}
            onDeleted={onDeleted}
          />
        ))}
      </div>
    </div>
  )
}
