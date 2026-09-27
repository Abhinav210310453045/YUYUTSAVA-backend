import React, { useState } from 'react'
import { deleteTodoNote, patchTodoNote } from '../../../api/client'
import { humanAge } from '../shared'
import { NOTE_DND, btnStyle, hasFiles, mono } from './common'

// One note: its author badge, age, body and edit/delete, and the drag that
// moves it between homes.

// Per-author badge tint — mirrors the session rows' ORIGIN_ACCENT hues.
const AUTHOR_COLOR = {
  user: {
    fg: 'var(--neon-green)',
    bg: 'rgba(var(--accent-rgb),0.10)',
    border: 'rgba(var(--accent-rgb),0.25)',
  },
  tinker: {
    fg: 'var(--text-info)',
    bg: 'rgba(120,160,255,0.12)',
    border: 'rgba(120,160,255,0.25)',
  },
  master: { fg: 'var(--neon-amber)', bg: 'rgba(251,191,36,0.10)', border: 'rgba(251,191,36,0.25)' },
}

export default function NoteCard({
  cardId,
  note,
  accent = null,
  onCardChange,
  selectMode,
  selected,
  onToggleSelect,
  onDragState,
  onFilesDropped,
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(note.body)
  const [busy, setBusy] = useState(false)
  const [fileOver, setFileOver] = useState(false)
  const author = AUTHOR_COLOR[note.author] || AUTHOR_COLOR.user

  const onSave = async () => {
    const body = draft.trim()
    if (!body || busy) return
    if (body === note.body) {
      setEditing(false)
      return
    }
    setBusy(true)
    try {
      const updated = await patchTodoNote(cardId, note.note_id, body)
      onCardChange((c) => ({
        ...c,
        notes: c.notes.map((n) => (n.note_id === updated.note_id ? updated : n)),
      }))
      setEditing(false)
    } catch (e) {
      alert(`Save failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  const onDelete = async () => {
    if (!confirm('Delete this note?')) return
    setBusy(true)
    try {
      await deleteTodoNote(cardId, note.note_id)
      onCardChange((c) => ({ ...c, notes: c.notes.filter((n) => n.note_id !== note.note_id) }))
    } catch (e) {
      alert(`Delete failed: ${e.message}`)
      setBusy(false)
    }
  }

  return (
    <div
      draggable={!editing}
      onDragStart={(e) => {
        e.stopPropagation()
        e.dataTransfer.setData(NOTE_DND, note.note_id)
        e.dataTransfer.setData('text/plain', note.note_id)
        e.dataTransfer.effectAllowed = 'move'
        onDragState?.(true)
      }}
      onDragEnd={() => onDragState?.(false)}
      onDragOver={(e) => {
        if (!hasFiles(e)) return
        e.preventDefault()
        e.stopPropagation()
        setFileOver(true)
      }}
      onDragLeave={() => setFileOver(false)}
      onDrop={(e) => {
        if (!hasFiles(e)) return
        e.preventDefault()
        e.stopPropagation()
        setFileOver(false)
        onFilesDropped?.(e.dataTransfer.files, {
          noteId: note.note_id,
          objectiveId: note.objective_id || undefined,
        })
      }}
      style={{
        background: 'var(--bg-elevated, #1a1a2e)',
        border: fileOver
          ? '1px dashed rgba(var(--accent-rgb),0.6)'
          : `1px solid ${selected ? accent?.bar || 'var(--text-info)' : 'var(--border-card)'}`,
        borderRadius: 8,
        padding: '8px 10px',
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
        ...mono,
        fontSize: 12,
        cursor: editing ? 'auto' : 'grab',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {selectMode && (
          <input
            type="checkbox"
            checked={selected}
            onChange={() => onToggleSelect?.(note.note_id)}
            style={{ accentColor: accent?.bar || 'var(--text-info)', margin: 0, cursor: 'pointer' }}
          />
        )}
        <span
          style={{
            fontSize: 8,
            padding: '1px 5px',
            borderRadius: 8,
            background: author.bg,
            color: author.fg,
            border: `1px solid ${author.border}`,
            textTransform: 'uppercase',
            letterSpacing: '0.05em',
          }}
        >
          {note.author}
        </span>
        <span style={{ color: 'var(--text-dim)', fontSize: 9 }}>{humanAge(note.updated_ts)}</span>
        <span style={{ flex: 1 }} />
        {!editing && (
          <>
            <button
              onClick={() => {
                setDraft(note.body)
                setEditing(true)
              }}
              disabled={busy}
              style={btnStyle('var(--text-info)', 'rgba(120,160,255,0.3)')}
            >
              ✎
            </button>
            <button
              onClick={onDelete}
              disabled={busy}
              style={btnStyle('var(--neon-red)', 'rgba(255,51,102,0.25)')}
            >
              {busy ? '…' : '✕'}
            </button>
          </>
        )}
      </div>

      {editing ? (
        <>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            autoFocus
            rows={Math.min(8, Math.max(3, draft.split('\n').length))}
            style={{
              background: 'var(--bg-card)',
              color: 'var(--text-primary)',
              border: '1px solid var(--border-card)',
              borderRadius: 6,
              padding: '6px 8px',
              ...mono,
              fontSize: 12,
              resize: 'vertical',
              outline: 'none',
            }}
          />
          <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
            <button
              onClick={() => setEditing(false)}
              disabled={busy}
              style={btnStyle('var(--text-muted)', 'var(--border-card)')}
            >
              Cancel
            </button>
            <button
              onClick={onSave}
              disabled={busy || !draft.trim()}
              style={{
                ...btnStyle('var(--neon-green)', 'rgba(var(--accent-rgb),0.25)'),
                background: 'rgba(var(--accent-rgb),0.08)',
              }}
            >
              {busy ? '…' : 'Save'}
            </button>
          </div>
        </>
      ) : (
        <div
          style={{
            color: 'var(--text-primary)',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            opacity: 0.9,
          }}
        >
          {note.body}
        </div>
      )}
    </div>
  )
}
