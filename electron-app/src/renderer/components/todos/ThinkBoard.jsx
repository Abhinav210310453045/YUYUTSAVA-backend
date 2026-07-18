import React, { useState, useCallback } from 'react'
import {
  addTodoNote, patchTodoNote, deleteTodoNote, assignTodoNote,
  patchTodoObjective, deleteTodoObjective,
} from '../../api/client'
import { PHASES, PHASE_ACCENT, humanAge } from './shared'
import ResizeHandle from '../common/ResizeHandle'
import { useDictation } from '../../hooks/useDictation'

// The card's whole thinking surface as one horizontally scrolling board:
// a General Notes column (free notes) first, then one column per think-flow
// phase. Objectives are cards stacked vertically inside their phase column;
// notes live inside their objective's card (or in General Notes) and drag
// between homes. Three drag species share the board, disambiguated by
// dataTransfer type — objectives move phase, notes move assignment, OS files
// attach — with stopPropagation so inner drops never bubble into a phase move.

const mono = { fontFamily: 'var(--font-mono)' }

const OBJECTIVE_DND = 'application/x-yy-objective'
const NOTE_DND = 'application/x-yy-note'

const COL_W_KEY = 'yy.todo.colW'
const COL_MIN = 240
const COL_MAX = 560

// Per-author badge tint — mirrors the session rows' ORIGIN_ACCENT hues.
export const AUTHOR_COLOR = {
  user: { fg: 'var(--neon-green)', bg: 'rgba(var(--accent-rgb),0.10)', border: 'rgba(var(--accent-rgb),0.25)' },
  tinker: { fg: 'var(--text-info)', bg: 'rgba(120,160,255,0.12)', border: 'rgba(120,160,255,0.25)' },
  master: { fg: 'var(--neon-amber)', bg: 'rgba(251,191,36,0.10)', border: 'rgba(251,191,36,0.25)' },
}

const btnStyle = (color, borderColor) => ({
  ...mono, fontSize: 10, padding: '3px 9px',
  background: 'transparent', color,
  border: `1px solid ${borderColor}`, borderRadius: 6, cursor: 'pointer',
})

const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes('Files')
const hasType = (e, t) => Array.from(e.dataTransfer?.types || []).includes(t)

// ── one note ───────────────────────────────────────────────────────────

function NoteCard({
  cardId, note, accent = null, onCardChange,
  selectMode, selected, onToggleSelect,
  onDragState, onFilesDropped,
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(note.body)
  const [busy, setBusy] = useState(false)
  const [fileOver, setFileOver] = useState(false)
  const author = AUTHOR_COLOR[note.author] || AUTHOR_COLOR.user

  const onSave = async () => {
    const body = draft.trim()
    if (!body || busy) return
    if (body === note.body) { setEditing(false); return }
    setBusy(true)
    try {
      const updated = await patchTodoNote(cardId, note.note_id, body)
      onCardChange((c) => ({
        ...c, notes: c.notes.map((n) => (n.note_id === updated.note_id ? updated : n)),
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
        e.preventDefault(); e.stopPropagation(); setFileOver(true)
      }}
      onDragLeave={() => setFileOver(false)}
      onDrop={(e) => {
        if (!hasFiles(e)) return
        e.preventDefault(); e.stopPropagation(); setFileOver(false)
        onFilesDropped?.(e.dataTransfer.files, { noteId: note.note_id, objectiveId: note.objective_id || undefined })
      }}
      style={{
        background: 'var(--bg-elevated, #1a1a2e)',
        border: fileOver
          ? '1px dashed rgba(var(--accent-rgb),0.6)'
          : `1px solid ${selected ? (accent?.bar || 'var(--text-info)') : 'var(--border-card)'}`,
        borderRadius: 8, padding: '8px 10px',
        display: 'flex', flexDirection: 'column', gap: 6,
        ...mono, fontSize: 12, cursor: editing ? 'auto' : 'grab',
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
        <span style={{
          fontSize: 8, padding: '1px 5px', borderRadius: 8,
          background: author.bg, color: author.fg, border: `1px solid ${author.border}`,
          textTransform: 'uppercase', letterSpacing: '0.05em',
        }}>
          {note.author}
        </span>
        <span style={{ color: 'var(--text-dim)', fontSize: 9 }}>{humanAge(note.updated_ts)}</span>
        <span style={{ flex: 1 }} />
        {!editing && (
          <>
            <button
              onClick={() => { setDraft(note.body); setEditing(true) }}
              disabled={busy}
              style={btnStyle('var(--text-info)', 'rgba(120,160,255,0.3)')}
            >
              ✎
            </button>
            <button onClick={onDelete} disabled={busy} style={btnStyle('var(--neon-red)', 'rgba(255,51,102,0.25)')}>
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
              background: 'var(--bg-card)', color: 'var(--text-primary)',
              border: '1px solid var(--border-card)', borderRadius: 6,
              padding: '6px 8px', ...mono, fontSize: 12, resize: 'vertical', outline: 'none',
            }}
          />
          <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
            <button onClick={() => setEditing(false)} disabled={busy} style={btnStyle('var(--text-muted)', 'var(--border-card)')}>
              Cancel
            </button>
            <button
              onClick={onSave}
              disabled={busy || !draft.trim()}
              style={{ ...btnStyle('var(--neon-green)', 'rgba(var(--accent-rgb),0.25)'), background: 'rgba(var(--accent-rgb),0.08)' }}
            >
              {busy ? '…' : 'Save'}
            </button>
          </div>
        </>
      ) : (
        <div style={{ color: 'var(--text-primary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word', opacity: 0.9 }}>
          {note.body}
        </div>
      )}
    </div>
  )
}

// ── note composer (shared by General Notes and objective cards) ────────

function NoteComposer({ cardId, objective = null, onCardChange, withMic = false, compact = false }) {
  const [draft, setDraft] = useState('')
  const [adding, setAdding] = useState(false)

  // STT dictation into the draft (never auto-submitted) — the mic renders
  // only in the General Notes composer, keeping one recorder per card view.
  const dictation = useDictation({
    onText: (text) => setDraft((cur) => {
      if (!cur) return text
      return /\s$/.test(cur) ? cur + text : `${cur} ${text}`
    }),
    onError: (e) => alert(`Dictation failed: ${e?.message || e}`),
  })

  const onAdd = async () => {
    const body = draft.trim()
    if (!body || adding) return
    setAdding(true)
    try {
      const note = await addTodoNote(cardId, body, 'user', objective ? {
        objectiveId: objective.objective_id, phase: objective.phase,
      } : {})
      onCardChange((c) => ({ ...c, notes: [...c.notes, note] }))
      setDraft('')
    } catch (e) {
      alert(`Add note failed: ${e.message}`)
    } finally {
      setAdding(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={objective ? 'note on this objective… (⌘/Ctrl+Enter)' : 'add a note… (⌘/Ctrl+Enter to save)'}
        rows={compact ? 2 : 3}
        onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) onAdd() }}
        style={{
          background: 'var(--bg-card)', color: 'var(--text-primary)',
          border: '1px solid var(--border-card)', borderRadius: 6,
          padding: '6px 8px', ...mono, fontSize: 12, resize: 'vertical', outline: 'none',
        }}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {withMic && (
          <button
            onClick={dictation.toggle}
            disabled={dictation.finishing}
            title={dictation.dictating ? 'stop dictating' : 'dictate a note (speech-to-text)'}
            style={{
              ...mono, fontSize: 10, padding: '4px 10px',
              background: dictation.dictating ? 'rgba(120,160,255,0.18)' : 'rgba(120,160,255,0.06)',
              color: 'var(--text-info)',
              border: `1px solid rgba(120,160,255,${dictation.dictating ? 0.5 : 0.3})`,
              borderRadius: 6,
              cursor: dictation.finishing ? 'default' : 'pointer',
              opacity: dictation.finishing ? 0.6 : 1,
            }}
          >
            {dictation.finishing ? '…' : dictation.dictating ? '● stop' : '🎙'}
          </button>
        )}
        {withMic && dictation.dictating && (
          <span style={{ ...mono, fontSize: 9, color: 'var(--neon-amber)' }}>listening…</span>
        )}
        <span style={{ flex: 1 }} />
        <button
          onClick={onAdd}
          disabled={adding || !draft.trim()}
          style={{
            ...mono, fontSize: 10, padding: '4px 12px',
            background: 'rgba(var(--accent-rgb),0.08)', color: 'var(--neon-green)',
            border: '1px solid rgba(var(--accent-rgb),0.25)', borderRadius: 6,
            cursor: adding || !draft.trim() ? 'default' : 'pointer',
            opacity: adding || !draft.trim() ? 0.5 : 1,
          }}
        >
          {adding ? '…' : '+ note'}
        </button>
      </div>
    </div>
  )
}

// ── one objective card ─────────────────────────────────────────────────

function ObjectiveCard({
  cardId, objective, notes, onCardChange,
  collapsed, onToggleCollapse,
  // onToggleSelect toggles THIS objective (board wraps it with the `obj:`
  // key prefix); onToggleNote toggles a nested note (board wraps `note:`).
  // Keep them separate — routing note ids through onToggleSelect would
  // double-prefix the key (`obj:note:<id>`) and break note selection.
  selectMode, selected, onToggleSelect, onToggleNote, selectedKeys,
  onDragState, onFilesDropped,
}) {
  const [title, setTitle] = useState(objective.title)
  const [reason, setReason] = useState(objective.reason || '')
  const [outcome, setOutcome] = useState(objective.outcome || '')
  const [busy, setBusy] = useState(false)
  const [over, setOver] = useState(null) // 'note' | 'file'
  const [editingTitle, setEditingTitle] = useState(false)
  const a = PHASE_ACCENT[objective.phase] || PHASE_ACCENT.abandoned

  const patch = useCallback(async (fields) => {
    setBusy(true)
    try {
      const updated = await patchTodoObjective(cardId, objective.objective_id, fields)
      onCardChange((c) => ({
        ...c,
        objectives: c.objectives.map((o) => (o.objective_id === updated.objective_id ? updated : o)),
      }))
    } catch (e) {
      alert(`Update failed: ${e.message}`)
    } finally {
      setBusy(false)
    }
  }, [cardId, objective.objective_id, onCardChange])

  const onDelete = async () => {
    if (!confirm('Delete this objective? Its notes stay on the card as general notes.')) return
    setBusy(true)
    try {
      await deleteTodoObjective(cardId, objective.objective_id)
      onCardChange((c) => ({
        ...c,
        objectives: c.objectives.filter((o) => o.objective_id !== objective.objective_id),
        notes: c.notes.map((n) => (
          n.objective_id === objective.objective_id ? { ...n, objective_id: null } : n
        )),
      }))
    } catch (e) {
      alert(`Delete failed: ${e.message}`)
      setBusy(false)
    }
  }

  const assignHere = async (noteId) => {
    try {
      const updated = await assignTodoNote(cardId, noteId, objective.objective_id, objective.phase)
      onCardChange((c) => ({
        ...c, notes: c.notes.map((n) => (n.note_id === updated.note_id ? updated : n)),
      }))
    } catch (e) {
      alert(`Assign failed: ${e.message}`)
    }
  }

  const fieldStyle = {
    ...mono, fontSize: 11, background: 'var(--bg-card)', color: 'var(--text-primary)',
    border: '1px solid var(--border-card)', borderRadius: 6, padding: '5px 8px',
    outline: 'none', resize: 'vertical',
  }

  return (
    <div
      onDragOver={(e) => {
        if (hasFiles(e)) { e.preventDefault(); e.stopPropagation(); setOver('file'); return }
        if (hasType(e, NOTE_DND)) { e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = 'move'; setOver('note') }
      }}
      onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setOver(null) }}
      onDrop={(e) => {
        if (hasFiles(e)) {
          e.preventDefault(); e.stopPropagation(); setOver(null)
          onFilesDropped?.(e.dataTransfer.files, { objectiveId: objective.objective_id })
          return
        }
        if (hasType(e, NOTE_DND)) {
          e.preventDefault(); e.stopPropagation(); setOver(null)
          const id = e.dataTransfer.getData(NOTE_DND)
          if (id) assignHere(id)
        }
      }}
      style={{
        background: 'var(--bg-elevated, #1a1a2e)',
        border: over === 'file'
          ? '1px dashed rgba(var(--accent-rgb),0.6)'
          : `1px ${over === 'note' ? 'dashed' : 'solid'} ${over === 'note' ? a.bar : (selected ? a.bar : a.border)}`,
        borderLeft: `3px solid ${a.bar}`,
        borderRadius: 8,
        display: 'flex', flexDirection: 'column',
        flexShrink: 0, minWidth: 0,
      }}
    >
      {/* Header = the drag handle. Cards drag phase-to-phase from here only,
          so textareas below never fight the HTML5 drag for text selection. */}
      <div
        draggable={!editingTitle}
        onDragStart={(e) => {
          e.dataTransfer.setData(OBJECTIVE_DND, objective.objective_id)
          e.dataTransfer.setData('text/plain', objective.objective_id)
          e.dataTransfer.effectAllowed = 'move'
          onDragState?.(true)
        }}
        onDragEnd={() => onDragState?.(false)}
        style={{
          display: 'flex', alignItems: 'center', gap: 6,
          padding: '8px 10px', cursor: 'grab', minWidth: 0,
        }}
      >
        {selectMode && (
          <input
            type="checkbox"
            checked={selected}
            onChange={() => onToggleSelect?.(objective.objective_id)}
            style={{ accentColor: a.bar, margin: 0, cursor: 'pointer' }}
          />
        )}
        <button
          onClick={() => onToggleCollapse?.(objective.objective_id)}
          title={collapsed ? 'expand' : 'collapse'}
          style={{
            ...mono, fontSize: 10, width: 18, height: 18, padding: 0,
            background: 'transparent', color: a.bar, border: 'none', cursor: 'pointer',
          }}
        >
          {collapsed ? '▸' : '▾'}
        </button>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onFocus={() => setEditingTitle(true)}
          onBlur={() => {
            setEditingTitle(false)
            const t = title.trim()
            if (t && t !== objective.title) patch({ title: t })
            else setTitle(objective.title)
          }}
          onKeyDown={(e) => e.key === 'Enter' && e.target.blur()}
          disabled={busy}
          title="click to rename"
          style={{
            ...mono, flex: 1, minWidth: 0, fontSize: 12, fontWeight: 'var(--fw-semibold)',
            background: 'transparent', color: 'var(--text-primary)',
            border: '1px solid transparent', borderRadius: 4, padding: '2px 4px',
            outline: 'none',
            textDecoration: objective.phase === 'abandoned' ? 'line-through' : 'none',
            opacity: objective.phase === 'abandoned' ? 0.6 : 1,
          }}
        />
        {notes.length > 0 && (
          <span style={{ ...mono, fontSize: 9, color: 'var(--text-dim)', whiteSpace: 'nowrap' }}>
            {notes.length} ✎
          </span>
        )}
        <select
          value={objective.phase}
          onChange={(e) => patch({ phase: e.target.value })}
          onMouseDown={(e) => e.stopPropagation()}
          disabled={busy}
          title="move to phase"
          style={{
            ...mono, fontSize: 9, background: a.glow, color: a.bar,
            border: `1px solid ${a.border}`, borderRadius: 6,
            padding: '2px 4px', cursor: 'pointer',
          }}
        >
          {PHASES.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <button onClick={onDelete} disabled={busy} title="delete objective" style={btnStyle('var(--neon-red)', 'rgba(255,51,102,0.25)')}>
          {busy ? '…' : '✕'}
        </button>
      </div>

      {!collapsed && (
        <div style={{
          display: 'flex', flexDirection: 'column', gap: 8,
          padding: '0 10px 10px', minWidth: 0,
        }}>
          {(objective.phase === 'blocked' || objective.phase === 'abandoned') && (
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              onBlur={() => { if (reason.trim() !== (objective.reason || '')) patch({ reason: reason.trim() }) }}
              placeholder={`why is this ${objective.phase}? (worth capturing for the journey)`}
              rows={2}
              disabled={busy}
              style={fieldStyle}
            />
          )}
          {objective.phase === 'completed' && (
            <textarea
              value={outcome}
              onChange={(e) => setOutcome(e.target.value)}
              onBlur={() => { if (outcome.trim() !== (objective.outcome || '')) patch({ outcome: outcome.trim() }) }}
              placeholder="outcome — what did completing this produce?"
              rows={2}
              disabled={busy}
              style={fieldStyle}
            />
          )}

          {notes.length > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: '40vh', overflowY: 'auto' }}>
              {notes.map((n) => (
                <NoteCard
                  key={n.note_id}
                  cardId={cardId}
                  note={n}
                  accent={a}
                  onCardChange={onCardChange}
                  selectMode={selectMode}
                  selected={selectedKeys?.has(`note:${n.note_id}`) ?? false}
                  onToggleSelect={onToggleNote}
                  onDragState={onDragState}
                  onFilesDropped={onFilesDropped}
                />
              ))}
            </div>
          )}

          <NoteComposer cardId={cardId} objective={objective} onCardChange={onCardChange} compact />
        </div>
      )}
    </div>
  )
}

// ── the board ──────────────────────────────────────────────────────────

export default function ThinkBoard({
  cardId, card, onCardChange, draggingRef,
  selectMode = false, selected, onToggleSelect,
  collapsedObjectives, onToggleCollapse,
  onFilesDropped,
}) {
  const [dragging, setDragging] = useState(false) // an objective or note is mid-drag
  const [overPhase, setOverPhase] = useState(null)
  const [generalOver, setGeneralOver] = useState(null) // 'note' | 'file'
  // One width for every column (General Notes included), resized from the
  // General Notes column's right edge and persisted app-wide.
  const [colW, setColW] = useState(() => {
    const v = Number(localStorage.getItem(COL_W_KEY))
    return v >= COL_MIN && v <= COL_MAX ? v : 320
  })
  const [colDragging, setColDragging] = useState(false)

  const onDragState = useCallback((active) => {
    setDragging(active)
    if (draggingRef) draggingRef.current = active
    if (!active) setOverPhase(null)
  }, [draggingRef])

  const freeNotes = (card.notes || []).filter((n) => !n.objective_id)
  const notesFor = (objectiveId) => (card.notes || []).filter((n) => n.objective_id === objectiveId)

  // Objective phase move — optimistic swap, poll gated by draggingRef,
  // revert on a failed PATCH (ported from the retired FlowBoard).
  const moveTo = useCallback(async (objectiveId, phase) => {
    const obj = card.objectives.find((o) => o.objective_id === objectiveId)
    if (!obj || obj.phase === phase) return
    const prev = obj.phase
    onCardChange((c) => ({
      ...c,
      objectives: c.objectives.map((o) => (o.objective_id === objectiveId ? { ...o, phase } : o)),
    }))
    try {
      const updated = await patchTodoObjective(cardId, objectiveId, { phase })
      onCardChange((c) => ({
        ...c,
        objectives: c.objectives.map((o) => (o.objective_id === updated.objective_id ? updated : o)),
      }))
    } catch (e) {
      alert(`Move failed: ${e.message}`)
      onCardChange((c) => ({
        ...c,
        objectives: c.objectives.map((o) => (o.objective_id === objectiveId ? { ...o, phase: prev } : o)),
      }))
    }
  }, [card.objectives, cardId, onCardChange])

  const detachNote = useCallback(async (noteId) => {
    try {
      const updated = await assignTodoNote(cardId, noteId, null)
      onCardChange((c) => ({
        ...c, notes: c.notes.map((n) => (n.note_id === updated.note_id ? updated : n)),
      }))
    } catch (e) {
      alert(`Detach failed: ${e.message}`)
    }
  }, [cardId, onCardChange])

  const startColDrag = useCallback((e) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = colW
    setColDragging(true)
    let w = startW
    const onMove = (ev) => {
      w = Math.min(COL_MAX, Math.max(COL_MIN, startW + (ev.clientX - startX)))
      setColW(w)
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      setColDragging(false)
      localStorage.setItem(COL_W_KEY, String(w))
    }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [colW])

  const colHeader = (label, accentColor, count) => (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
      <span style={{ width: 6, height: 6, borderRadius: '50%', background: accentColor, flexShrink: 0 }} />
      <span style={{ ...mono, fontSize: 10, color: accentColor, textTransform: 'uppercase', letterSpacing: '0.08em' }}>
        {label}
      </span>
      {count > 0 && <span style={{ ...mono, fontSize: 9, color: 'var(--text-dim)' }}>{count}</span>}
    </div>
  )

  return (
    <div style={{
      flex: 1, minHeight: 0,
      display: 'flex', gap: 8, alignItems: 'stretch',
      overflowX: 'auto', overflowY: 'hidden',
      paddingBottom: 6,
    }}>
      {/* ── General Notes: the free-note home and the detach drop target ── */}
      <div
        onDragOver={(e) => {
          if (hasFiles(e)) { e.preventDefault(); setGeneralOver('file'); return }
          if (hasType(e, NOTE_DND)) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; setGeneralOver('note') }
        }}
        onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setGeneralOver(null) }}
        onDrop={(e) => {
          if (hasFiles(e)) {
            e.preventDefault(); setGeneralOver(null)
            onFilesDropped?.(e.dataTransfer.files, {})
            return
          }
          if (hasType(e, NOTE_DND)) {
            e.preventDefault(); setGeneralOver(null)
            const id = e.dataTransfer.getData(NOTE_DND)
            if (id) detachNote(id)
          }
        }}
        style={{
          flex: `0 0 ${colW}px`, width: colW, minWidth: 0,
          display: 'flex', flexDirection: 'column', gap: 8, padding: 8,
          background: generalOver === 'note' ? 'rgba(var(--accent-rgb),0.05)' : 'var(--bg-card)',
          border: generalOver === 'file'
            ? '1px dashed rgba(var(--accent-rgb),0.6)'
            : `1px ${generalOver === 'note' ? 'dashed' : 'solid'} ${generalOver === 'note' ? 'var(--neon-green)' : 'var(--border-subtle)'}`,
          borderRadius: 10,
        }}
      >
        {colHeader('general notes', 'var(--neon-green)', freeNotes.length)}
        <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', gap: 8, overflowY: 'auto' }}>
          {freeNotes.length === 0 && (
            <span style={{ ...mono, fontSize: 11, color: 'var(--text-muted)' }}>
              {'> think on paper here — or drop a note back to detach it'}
            </span>
          )}
          {freeNotes.map((n) => (
            <NoteCard
              key={n.note_id}
              cardId={cardId}
              note={n}
              onCardChange={onCardChange}
              selectMode={selectMode}
              selected={selected?.has(`note:${n.note_id}`) ?? false}
              onToggleSelect={(id) => onToggleSelect?.(`note:${id}`)}
              onDragState={onDragState}
              onFilesDropped={onFilesDropped}
            />
          ))}
        </div>
        <NoteComposer cardId={cardId} onCardChange={onCardChange} withMic />
      </div>

      {/* Column-width slider for the whole board. */}
      <ResizeHandle onMouseDown={startColDrag} side="left" />

      {/* ── one column per phase ── */}
      {PHASES.map((phase) => {
        const a = PHASE_ACCENT[phase]
        const inPhase = card.objectives.filter((o) => o.phase === phase)
        // Empty phases collapse to slim strips; a hovering objective drag
        // re-expands them so they stay valid drop targets.
        const collapsed = inPhase.length === 0 && overPhase !== phase
        const isOver = overPhase === phase && dragging
        return (
          <div
            key={phase}
            id={phase === 'thinking' ? 'yy-phase-thinking' : undefined}
            onDragOver={(e) => {
              if (!hasType(e, OBJECTIVE_DND)) return
              e.preventDefault(); e.dataTransfer.dropEffect = 'move'; setOverPhase(phase)
            }}
            onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setOverPhase((p) => (p === phase ? null : p)) }}
            onDrop={(e) => {
              if (!hasType(e, OBJECTIVE_DND)) return
              e.preventDefault()
              const id = e.dataTransfer.getData(OBJECTIVE_DND)
              onDragState(false)
              if (id) moveTo(id, phase)
            }}
            style={{
              flex: collapsed ? '0 0 34px' : `0 0 ${colW}px`,
              width: collapsed ? 34 : colW,
              display: 'flex', flexDirection: 'column', gap: 8, padding: 8,
              background: isOver ? a.glow : 'var(--bg-card)',
              border: `1px ${isOver ? 'dashed' : 'solid'} ${isOver ? a.bar : 'var(--border-subtle)'}`,
              borderRadius: 10,
              transition: 'flex-basis 0.2s ease, background 0.15s ease',
              overflow: 'hidden',
            }}
          >
            {collapsed ? (
              <div style={{
                ...mono, fontSize: 9, color: a.bar, textTransform: 'uppercase',
                letterSpacing: '0.1em', writingMode: 'vertical-rl',
                margin: '4px auto 0', opacity: 0.7, whiteSpace: 'nowrap',
              }}>
                {phase}
              </div>
            ) : (
              <>
                {colHeader(phase, a.bar, inPhase.length)}
                <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', gap: 8, overflowY: 'auto' }}>
                  {inPhase.map((o) => (
                    <ObjectiveCard
                      key={o.objective_id}
                      cardId={cardId}
                      objective={o}
                      notes={notesFor(o.objective_id)}
                      onCardChange={onCardChange}
                      collapsed={collapsedObjectives?.has(o.objective_id) ?? false}
                      onToggleCollapse={onToggleCollapse}
                      selectMode={selectMode}
                      selected={selected?.has(`obj:${o.objective_id}`) ?? false}
                      onToggleSelect={(id) => onToggleSelect?.(`obj:${id}`)}
                      onToggleNote={(id) => onToggleSelect?.(`note:${id}`)}
                      selectedKeys={selected}
                      onDragState={onDragState}
                      onFilesDropped={onFilesDropped}
                    />
                  ))}
                </div>
              </>
            )}
          </div>
        )
      })}
    </div>
  )
}
