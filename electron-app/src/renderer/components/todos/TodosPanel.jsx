import React, { useEffect, useState, useCallback } from 'react'
import { listTodos, createTodo, deleteTodo } from '../../api/client'
import TodoCardView from './TodoCardView'
import { STATUS_ACCENT, PHASE_ACCENT, TagChips, PinIcon, humanAge } from './shared'
import { useNav } from '../../nav/NavProvider'
import { dropViewState, useScrollRestore } from '../../nav/useViewState'
import './TodosPanel.css'

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
    <span title={label} className="todo-card-count">
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
      className="hover-bulge todo-card"
      onClick={() => onOpen?.(card.card_id)}
      title="click to open this card"
      style={{
        border: `1px solid ${accent.border}`,
        borderLeft: `3px solid ${accent.bar}`,
        boxShadow: `0 2px 10px rgba(0, 0, 0, 0.45), 0 0 16px ${accent.glow}`,
        '--bulge-glow': accent.hover,
      }}
    >
      <div className="todo-card-head">
        {card.pinned && (
          <span title="pinned" className="todo-card-pin">
            <PinIcon />
          </span>
        )}
        <span className="todo-card-title">{card.title}</span>
        <button
          onClick={onDelete}
          disabled={deleting}
          title="delete card"
          className="todo-card-delete"
          style={{ cursor: deleting ? 'default' : 'pointer', opacity: deleting ? 0.5 : 1 }}
        >
          {deleting ? '...' : '✕'}
        </button>
      </div>

      <TagChips tags={card.tags} />

      {/* Think-flow progress: completed / total objectives (summary counts). */}
      {card.objective_count > 0 && (
        <div className="todo-card-progress">
          <div className="todo-card-track">
            <div
              className="todo-card-fill"
              style={{
                width: `${Math.round((card.objective_done_count / card.objective_count) * 100)}%`,
                background: PHASE_ACCENT.completed.bar,
              }}
            />
          </div>
          <span className="todo-card-ratio">
            {card.objective_done_count}/{card.objective_count}
          </span>
        </div>
      )}

      <div className="todo-card-foot">
        <CountBadge label="notes" count={card.note_count} />
        <span className="todo-card-sep">·</span>
        <CountBadge label="files" count={card.attachment_count} />
        <span className="todo-card-spacer" />
        <span className="todo-card-age">{humanAge(card.updated_ts)}</span>
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
    <div className="todos-col">
      <div className="todos-col-head">
        <span className="todos-col-dot" style={{ background: accent.bar }} />
        <h2 className="todos-col-title">{col.title}</h2>
        {cards.length > 0 && (
          <span
            className="todos-col-count"
            style={{
              color: accent.bar,
              background: accent.glow,
              border: `1px solid ${accent.border}`,
            }}
          >
            {cards.length}
          </span>
        )}
      </div>

      <div ref={scrollRef} className="todos-col-cards">
        {error && <div className="todos-error">{`> todos endpoint: ${error}`}</div>}

        {isEmpty && !error && (
          <div className="todos-empty">
            <div className="todos-empty-glyph">☐</div>
            <div>{'> none'}</div>
            <div className="todos-empty-hint">{col.empty}</div>
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
    byStatus[key].sort((a, b) => b.pinned - a.pinned || b.updated_ts - a.updated_ts)
  }

  return (
    <div className="todos">
      <div className="todos-bar">
        <span className="todos-title">Todos — board</span>
        <input
          value={newTitle}
          onChange={(e) => setNewTitle(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onCreate()
          }}
          placeholder="new TODO title…"
          className="todos-new"
        />
        <button
          onClick={onCreate}
          disabled={creating || !newTitle.trim()}
          className="todos-bar-btn"
          style={{
            cursor: creating || !newTitle.trim() ? 'default' : 'pointer',
            opacity: creating || !newTitle.trim() ? 0.5 : 1,
          }}
        >
          {creating ? '...' : '+ Add'}
        </button>
        <button onClick={refresh} title="Refresh" className="todos-bar-btn todos-bar-btn--icon">
          ↻
        </button>
      </div>

      <div className="todos-columns">
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
