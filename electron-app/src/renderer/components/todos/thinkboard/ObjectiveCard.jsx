import React, { useState } from 'react'
import { PHASES, PHASE_ACCENT } from '../shared'
import { NOTE_DND, OBJECTIVE_DND, btnStyle, hasFiles, hasType, mono } from './common'
import NoteCard from './NoteCard'
import NoteComposer from './NoteComposer'
import { useObjectiveActions } from './useObjectiveActions'

// One objective: the header is its drag handle (phase moves), the body holds
// the reason/outcome field, its notes, and a composer. Notes and OS files
// drop onto it.

export default function ObjectiveCard({
  cardId,
  objective,
  notes,
  onCardChange,
  collapsed,
  onToggleCollapse,
  // onToggleSelect toggles THIS objective (board wraps it with the `obj:`
  // key prefix); onToggleNote toggles a nested note (board wraps `note:`).
  // Keep them separate — routing note ids through onToggleSelect would
  // double-prefix the key (`obj:note:<id>`) and break note selection.
  selectMode,
  selected,
  onToggleSelect,
  onToggleNote,
  selectedKeys,
  onDragState,
  onFilesDropped,
}) {
  const [title, setTitle] = useState(objective.title)
  const [reason, setReason] = useState(objective.reason || '')
  const [outcome, setOutcome] = useState(objective.outcome || '')
  const [over, setOver] = useState(null) // 'note' | 'file'
  const [editingTitle, setEditingTitle] = useState(false)
  const a = PHASE_ACCENT[objective.phase] || PHASE_ACCENT.abandoned

  const { busy, patch, onDelete, assignHere } = useObjectiveActions(cardId, objective, onCardChange)

  const fieldStyle = {
    ...mono,
    fontSize: 11,
    background: 'var(--bg-card)',
    color: 'var(--text-primary)',
    border: '1px solid var(--border-card)',
    borderRadius: 6,
    padding: '5px 8px',
    outline: 'none',
    resize: 'vertical',
  }

  return (
    <div
      onDragOver={(e) => {
        if (hasFiles(e)) {
          e.preventDefault()
          e.stopPropagation()
          setOver('file')
          return
        }
        if (hasType(e, NOTE_DND)) {
          e.preventDefault()
          e.stopPropagation()
          e.dataTransfer.dropEffect = 'move'
          setOver('note')
        }
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget)) setOver(null)
      }}
      onDrop={(e) => {
        if (hasFiles(e)) {
          e.preventDefault()
          e.stopPropagation()
          setOver(null)
          onFilesDropped?.(e.dataTransfer.files, { objectiveId: objective.objective_id })
          return
        }
        if (hasType(e, NOTE_DND)) {
          e.preventDefault()
          e.stopPropagation()
          setOver(null)
          const id = e.dataTransfer.getData(NOTE_DND)
          if (id) assignHere(id)
        }
      }}
      style={{
        background: 'var(--bg-elevated, #1a1a2e)',
        border:
          over === 'file'
            ? '1px dashed rgba(var(--accent-rgb),0.6)'
            : `1px ${over === 'note' ? 'dashed' : 'solid'} ${over === 'note' ? a.bar : selected ? a.bar : a.border}`,
        borderLeft: `3px solid ${a.bar}`,
        borderRadius: 8,
        display: 'flex',
        flexDirection: 'column',
        flexShrink: 0,
        minWidth: 0,
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
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          padding: '8px 10px',
          cursor: 'grab',
          minWidth: 0,
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
            ...mono,
            fontSize: 10,
            width: 18,
            height: 18,
            padding: 0,
            background: 'transparent',
            color: a.bar,
            border: 'none',
            cursor: 'pointer',
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
            ...mono,
            flex: 1,
            minWidth: 0,
            fontSize: 12,
            fontWeight: 'var(--fw-semibold)',
            background: 'transparent',
            color: 'var(--text-primary)',
            border: '1px solid transparent',
            borderRadius: 4,
            padding: '2px 4px',
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
            ...mono,
            fontSize: 9,
            background: a.glow,
            color: a.bar,
            border: `1px solid ${a.border}`,
            borderRadius: 6,
            padding: '2px 4px',
            cursor: 'pointer',
          }}
        >
          {PHASES.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <button
          onClick={onDelete}
          disabled={busy}
          title="delete objective"
          style={btnStyle('var(--neon-red)', 'rgba(255,51,102,0.25)')}
        >
          {busy ? '…' : '✕'}
        </button>
      </div>

      {!collapsed && (
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
            padding: '0 10px 10px',
            minWidth: 0,
          }}
        >
          {(objective.phase === 'blocked' || objective.phase === 'abandoned') && (
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              onBlur={() => {
                if (reason.trim() !== (objective.reason || '')) patch({ reason: reason.trim() })
              }}
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
              onBlur={() => {
                if (outcome.trim() !== (objective.outcome || '')) patch({ outcome: outcome.trim() })
              }}
              placeholder="outcome — what did completing this produce?"
              rows={2}
              disabled={busy}
              style={fieldStyle}
            />
          )}

          {notes.length > 0 && (
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                gap: 6,
                maxHeight: '40vh',
                overflowY: 'auto',
              }}
            >
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
