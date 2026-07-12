import React, { useEffect, useState, useCallback, useRef } from 'react'
import { getTodo, patchTodo, addTodoNote, patchTodoNote, deleteTodoNote } from '../../api/client'
import { STATUS_ACCENT, TagChips, PinIcon, humanAge } from './shared'
import ChatPanel from '../chat/ChatPanel'

const STATUSES = ['inbox', 'active', 'done', 'archived']

// Per-author badge tint: user green (theirs), tinker blue, master amber —
// mirrors the ORIGIN_ACCENT hue assignments used on session rows.
const AUTHOR_COLOR = {
  user: { fg: '#00ff88', bg: 'rgba(0,255,136,0.10)', border: 'rgba(0,255,136,0.25)' },
  tinker: { fg: '#9bb8ff', bg: 'rgba(120,160,255,0.12)', border: 'rgba(120,160,255,0.25)' },
  master: { fg: '#fbbf24', bg: 'rgba(251,191,36,0.10)', border: 'rgba(251,191,36,0.25)' },
}

const noteBtnStyle = (color, borderColor) => ({
  fontFamily: 'var(--font-mono)',
  fontSize: 10,
  padding: '3px 9px',
  background: 'transparent',
  color,
  border: `1px solid ${borderColor}`,
  borderRadius: 6,
  cursor: 'pointer',
})

function NoteRow({ note, onChanged, onDeleted }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(note.body)
  const [busy, setBusy] = useState(false)
  const author = AUTHOR_COLOR[note.author] || AUTHOR_COLOR.user

  const onSave = async () => {
    const body = draft.trim()
    if (!body || busy) return
    if (body === note.body) { setEditing(false); return }
    setBusy(true)
    try {
      const updated = await patchTodoNote(note.card_id, note.note_id, body)
      onChanged?.(updated)
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
      await deleteTodoNote(note.card_id, note.note_id)
      onDeleted?.(note.note_id)
    } catch (e) {
      alert(`Delete failed: ${e.message}`)
      setBusy(false)
    }
  }

  return (
    <div style={{
      background: 'var(--bg-elevated, #1a1a2e)',
      border: '1px solid var(--border-card)',
      borderRadius: 8,
      padding: '10px 14px',
      display: 'flex',
      flexDirection: 'column',
      gap: 8,
      fontFamily: 'var(--font-mono)',
      fontSize: 12,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{
          fontSize: 9,
          padding: '1px 6px',
          borderRadius: 8,
          background: author.bg,
          color: author.fg,
          border: `1px solid ${author.border}`,
          textTransform: 'uppercase',
          letterSpacing: '0.05em',
        }}>
          {note.author}
        </span>
        <span style={{ color: 'var(--text-dim)', fontSize: 10 }}>{humanAge(note.updated_ts)}</span>
        <span style={{ flex: 1 }} />
        {!editing && (
          <>
            <button
              onClick={() => { setDraft(note.body); setEditing(true) }}
              disabled={busy}
              style={noteBtnStyle('#9bb8ff', 'rgba(120,160,255,0.3)')}
            >
              Edit
            </button>
            <button
              onClick={onDelete}
              disabled={busy}
              style={noteBtnStyle('var(--neon-red)', 'rgba(255,51,102,0.25)')}
            >
              {busy ? '...' : 'Delete'}
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
            rows={Math.min(10, Math.max(3, draft.split('\n').length))}
            style={{
              background: 'var(--bg-card)',
              color: 'var(--text-primary)',
              border: '1px solid var(--border-card)',
              borderRadius: 6,
              padding: '8px 10px',
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
              resize: 'vertical',
              outline: 'none',
            }}
          />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button onClick={() => setEditing(false)} disabled={busy} style={noteBtnStyle('var(--text-muted)', 'var(--border-card)')}>
              Cancel
            </button>
            <button
              onClick={onSave}
              disabled={busy || !draft.trim()}
              style={{ ...noteBtnStyle('var(--neon-green)', 'rgba(0,255,136,0.25)'), background: 'rgba(0,255,136,0.08)' }}
            >
              {busy ? '...' : 'Save'}
            </button>
          </div>
        </>
      ) : (
        <div style={{
          color: 'var(--text-primary)',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          opacity: 0.9,
        }}>
          {note.body}
        </div>
      )}
    </div>
  )
}

export default function TodoCardView({ cardId, onBack }) {
  const [card, setCard] = useState(null)
  const [error, setError] = useState(null)
  const [title, setTitle] = useState('')
  const [newNote, setNewNote] = useState('')
  const [addingNote, setAddingNote] = useState(false)
  const [patching, setPatching] = useState(false)
  // "Think with TinkerAgent" split: when open, the content area becomes
  // notes | chat. The chat is the shared ChatPanel pointed at agent=tinker —
  // its thread is pinned server-side to this card, so it resumes on reopen.
  const [thinkOpen, setThinkOpen] = useState(false)
  const titleRef = useRef(null)
  // Esc sets this before blurring: the blur handler must skip the commit
  // because the reverted title state hasn't re-rendered into its closure yet.
  const revertingRef = useRef(false)

  const refresh = useCallback(async () => {
    try {
      const c = await getTodo(cardId)
      setCard(c)
      setTitle(c.title)
      setError(null)
    } catch (e) {
      setError(e.message)
    }
  }, [cardId])

  useEffect(() => { refresh() }, [refresh])

  // Shared partial-update path: PATCH, then swap in the returned card (the
  // response is the fully hydrated TodoCardV1, notes included).
  const patch = useCallback(async (fields) => {
    setPatching(true)
    try {
      const c = await patchTodo(cardId, fields)
      setCard(c)
      setTitle(c.title)
    } catch (e) {
      alert(`Update failed: ${e.message}`)
    } finally {
      setPatching(false)
    }
  }, [cardId])

  const onTitleCommit = () => {
    if (revertingRef.current) { revertingRef.current = false; setTitle(card?.title || ''); return }
    const t = title.trim()
    if (!card || !t || t === card.title) { setTitle(card?.title || ''); return }
    patch({ title: t })
  }

  const onAddNote = async () => {
    const body = newNote.trim()
    if (!body || addingNote) return
    setAddingNote(true)
    try {
      const note = await addTodoNote(cardId, body)
      setCard((c) => (c ? { ...c, notes: [...c.notes, note] } : c))
      setNewNote('')
    } catch (e) {
      alert(`Add note failed: ${e.message}`)
    } finally {
      setAddingNote(false)
    }
  }

  const onNoteChanged = useCallback((updated) => {
    setCard((c) => (c ? {
      ...c,
      notes: c.notes.map((n) => (n.note_id === updated.note_id ? updated : n)),
    } : c))
  }, [])

  const onNoteDeleted = useCallback((noteId) => {
    setCard((c) => (c ? { ...c, notes: c.notes.filter((n) => n.note_id !== noteId) } : c))
  }, [])

  const accent = STATUS_ACCENT[card?.status] || STATUS_ACCENT.inbox

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '14px 24px', borderBottom: '1px solid var(--border-subtle)',
      }}>
        <button
          onClick={onBack}
          title="back to board"
          style={{
            fontFamily: 'var(--font-mono)', fontSize: 11, padding: '5px 12px',
            background: 'transparent', color: 'var(--text-muted)',
            border: '1px solid var(--border-card)', borderRadius: 6, cursor: 'pointer',
          }}
        >
          ← Board
        </button>

        {card && (
          <>
            {/* Renamable title — commit on Enter or blur, Esc reverts. */}
            <input
              ref={titleRef}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              onBlur={onTitleCommit}
              onKeyDown={(e) => {
                if (e.key === 'Enter') titleRef.current?.blur()
                if (e.key === 'Escape') { revertingRef.current = true; titleRef.current?.blur() }
              }}
              disabled={patching}
              title="click to rename"
              style={{
                flex: 1, minWidth: 0,
                background: 'transparent', color: 'var(--text-primary)',
                border: '1px solid transparent', borderRadius: 6, padding: '5px 10px',
                fontFamily: 'var(--font-mono)', fontSize: 13, fontWeight: 600,
                outline: 'none',
              }}
              onFocus={(e) => { e.target.style.border = '1px solid var(--border-card)'; e.target.style.background = 'var(--bg-card)' }}
              onBlurCapture={(e) => { e.target.style.border = '1px solid transparent'; e.target.style.background = 'transparent' }}
            />

            <button
              onClick={() => patch({ pinned: !card.pinned })}
              disabled={patching}
              title={card.pinned ? 'unpin' : 'pin to top'}
              style={{
                width: 28, height: 28, borderRadius: 6,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                background: card.pinned ? 'rgba(250,204,21,0.10)' : 'transparent',
                border: `1px solid ${card.pinned ? 'rgba(250,204,21,0.3)' : 'var(--border-card)'}`,
                cursor: 'pointer',
              }}
            >
              <PinIcon color={card.pinned ? '#facc15' : 'var(--text-muted)'} />
            </button>

            <select
              value={card.status}
              onChange={(e) => patch({ status: e.target.value })}
              disabled={patching}
              title="card status"
              style={{
                background: 'var(--bg-card)', color: accent.bar,
                border: `1px solid ${accent.border}`, borderRadius: 6, padding: '5px 8px',
                fontFamily: 'var(--font-mono)', fontSize: 11, cursor: 'pointer',
              }}
            >
              {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>

            <button
              onClick={() => setThinkOpen((v) => !v)}
              title={thinkOpen ? 'close the TinkerAgent chat' : 'think on this card with the TinkerAgent'}
              style={{
                fontFamily: 'var(--font-mono)', fontSize: 11, padding: '5px 12px',
                background: thinkOpen ? 'rgba(120,160,255,0.18)' : 'rgba(120,160,255,0.06)',
                color: '#9bb8ff',
                border: `1px solid rgba(120,160,255,${thinkOpen ? 0.5 : 0.3})`,
                borderRadius: 6, cursor: 'pointer', whiteSpace: 'nowrap',
              }}
            >
              {thinkOpen ? '✕ Tinker' : '✦ Tinker'}
            </button>
          </>
        )}
      </div>

      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
      <div style={{ flex: 1, minWidth: 0, overflowY: 'auto', padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 16 }}>
        {error && (
          <div style={{
            fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--neon-red)',
            padding: '6px 10px', border: '1px solid rgba(255,51,102,0.25)',
            borderRadius: 6, background: 'rgba(255,51,102,0.05)',
          }}>
            {`> card: ${error}`}
          </div>
        )}

        {!card && !error && (
          <div style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>loading…</div>
        )}

        {card && (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-muted)' }}>
              <TagChips tags={card.tags} />
              <span style={{ flex: 1 }} />
              <span>created {humanAge(card.created_ts)}</span>
              <span style={{ color: 'var(--text-dim)' }}>·</span>
              <span>updated {humanAge(card.updated_ts)}</span>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <h2 style={{
                fontSize: 12, fontWeight: 600, fontFamily: 'var(--font-mono)',
                color: 'var(--text-primary)', textTransform: 'uppercase',
                letterSpacing: '0.1em', margin: 0,
              }}>
                Notes
              </h2>
              {card.notes.length > 0 && (
                <span style={{
                  fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--neon-green)',
                  background: 'rgba(0,255,136,0.08)', border: '1px solid rgba(0,255,136,0.2)',
                  borderRadius: 10, padding: '1px 7px',
                }}>
                  {card.notes.length}
                </span>
              )}
            </div>

            {card.notes.length === 0 && (
              <div style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                {'> no notes yet — think on paper below'}
              </div>
            )}

            {card.notes.map((n) => (
              <NoteRow key={n.note_id} note={n} onChanged={onNoteChanged} onDeleted={onNoteDeleted} />
            ))}

            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <textarea
                value={newNote}
                onChange={(e) => setNewNote(e.target.value)}
                placeholder="add a note… (⌘/Ctrl+Enter to save)"
                rows={3}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) onAddNote()
                }}
                style={{
                  background: 'var(--bg-card)', color: 'var(--text-primary)',
                  border: '1px solid var(--border-card)', borderRadius: 6,
                  padding: '8px 10px', fontFamily: 'var(--font-mono)', fontSize: 12,
                  resize: 'vertical', outline: 'none',
                }}
              />
              <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                <button
                  onClick={onAddNote}
                  disabled={addingNote || !newNote.trim()}
                  style={{
                    fontFamily: 'var(--font-mono)', fontSize: 11, padding: '6px 14px',
                    background: 'rgba(0,255,136,0.08)', color: 'var(--neon-green)',
                    border: '1px solid rgba(0,255,136,0.25)', borderRadius: 6,
                    cursor: addingNote || !newNote.trim() ? 'default' : 'pointer',
                    opacity: addingNote || !newNote.trim() ? 0.5 : 1,
                  }}
                >
                  {addingNote ? '...' : '+ Add note'}
                </button>
              </div>
            </div>
          </>
        )}
      </div>

      {thinkOpen && card && (
        <div style={{
          width: '46%', minWidth: 380, maxWidth: 640,
          borderLeft: '1px solid var(--border-subtle)',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
        }}>
          {/* The shared chat surface, unforked: agent=tinker + card pins the
              thread to todo:<card_id> server-side; resumeId hydrates past
              turns on reopen (best-effort 404 on a card's first chat).
              Voice toggle is Phase 5; the card IS the thread, so no New. */}
          <ChatPanel
            agent="tinker"
            card={cardId}
            origin="tinker"
            resumeId={`todo:${cardId}`}
            title="Think with TinkerAgent"
            placeholder="hand the TinkerAgent a rough idea… (Enter to send)"
            emptyGlyph="✦"
            emptyHint="> tinker on this card — it sharpens ideas, asks the right questions, and keeps notes here"
            showVoice={false}
            showNewSession={false}
            onTurnEnd={refresh}
          />
        </div>
      )}
      </div>
    </div>
  )
}
