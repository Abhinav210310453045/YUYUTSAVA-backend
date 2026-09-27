import React, { useState, useCallback } from 'react'
import { assignTodoNote, patchTodoObjective } from '../../api/client'
import { PHASES, PHASE_ACCENT } from './shared'
import ResizeHandle from '../common/ResizeHandle'
import ColumnHeader from './thinkboard/ColumnHeader'
import { NOTE_DND, OBJECTIVE_DND, hasFiles, hasType, mono } from './thinkboard/common'
import NoteCard from './thinkboard/NoteCard'
import NoteComposer from './thinkboard/NoteComposer'
import ObjectiveCard from './thinkboard/ObjectiveCard'
import { useColumnWidth } from './thinkboard/useColumnWidth'

// The card's whole thinking surface as one horizontally scrolling board:
// a General Notes column (free notes) first, then one column per think-flow
// phase. Objectives are cards stacked vertically inside their phase column;
// notes live inside their objective's card (or in General Notes) and drag
// between homes. Three drag species share the board, disambiguated by
// dataTransfer type — objectives move phase, notes move assignment, OS files
// attach — with stopPropagation so inner drops never bubble into a phase move.

export default function ThinkBoard({
  cardId,
  card,
  onCardChange,
  draggingRef,
  selectMode = false,
  selected,
  onToggleSelect,
  collapsedObjectives,
  onToggleCollapse,
  onFilesDropped,
}) {
  const [dragging, setDragging] = useState(false) // an objective or note is mid-drag
  const [overPhase, setOverPhase] = useState(null)
  const [generalOver, setGeneralOver] = useState(null) // 'note' | 'file'
  // One width for every column (General Notes included), resized from the
  // General Notes column's right edge and persisted app-wide.
  const { colW, startColDrag } = useColumnWidth()

  const onDragState = useCallback(
    (active) => {
      setDragging(active)
      if (draggingRef) draggingRef.current = active
      if (!active) setOverPhase(null)
    },
    [draggingRef],
  )

  const freeNotes = (card.notes || []).filter((n) => !n.objective_id)
  const notesFor = (objectiveId) => (card.notes || []).filter((n) => n.objective_id === objectiveId)

  // Objective phase move — optimistic swap, poll gated by draggingRef,
  // revert on a failed PATCH (ported from the retired FlowBoard).
  const moveTo = useCallback(
    async (objectiveId, phase) => {
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
          objectives: c.objectives.map((o) =>
            o.objective_id === updated.objective_id ? updated : o,
          ),
        }))
      } catch (e) {
        alert(`Move failed: ${e.message}`)
        onCardChange((c) => ({
          ...c,
          objectives: c.objectives.map((o) =>
            o.objective_id === objectiveId ? { ...o, phase: prev } : o,
          ),
        }))
      }
    },
    [card.objectives, cardId, onCardChange],
  )

  const detachNote = useCallback(
    async (noteId) => {
      try {
        const updated = await assignTodoNote(cardId, noteId, null)
        onCardChange((c) => ({
          ...c,
          notes: c.notes.map((n) => (n.note_id === updated.note_id ? updated : n)),
        }))
      } catch (e) {
        alert(`Detach failed: ${e.message}`)
      }
    },
    [cardId, onCardChange],
  )

  return (
    <div
      style={{
        flex: 1,
        minHeight: 0,
        display: 'flex',
        gap: 8,
        alignItems: 'stretch',
        overflowX: 'auto',
        overflowY: 'hidden',
        paddingBottom: 6,
      }}
    >
      {/* ── General Notes: the free-note home and the detach drop target ── */}
      <div
        onDragOver={(e) => {
          if (hasFiles(e)) {
            e.preventDefault()
            setGeneralOver('file')
            return
          }
          if (hasType(e, NOTE_DND)) {
            e.preventDefault()
            e.dataTransfer.dropEffect = 'move'
            setGeneralOver('note')
          }
        }}
        onDragLeave={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget)) setGeneralOver(null)
        }}
        onDrop={(e) => {
          if (hasFiles(e)) {
            e.preventDefault()
            setGeneralOver(null)
            onFilesDropped?.(e.dataTransfer.files, {})
            return
          }
          if (hasType(e, NOTE_DND)) {
            e.preventDefault()
            setGeneralOver(null)
            const id = e.dataTransfer.getData(NOTE_DND)
            if (id) detachNote(id)
          }
        }}
        style={{
          flex: `0 0 ${colW}px`,
          width: colW,
          minWidth: 0,
          display: 'flex',
          flexDirection: 'column',
          gap: 8,
          padding: 8,
          background: generalOver === 'note' ? 'rgba(var(--accent-rgb),0.05)' : 'var(--bg-card)',
          border:
            generalOver === 'file'
              ? '1px dashed rgba(var(--accent-rgb),0.6)'
              : `1px ${generalOver === 'note' ? 'dashed' : 'solid'} ${generalOver === 'note' ? 'var(--neon-green)' : 'var(--border-subtle)'}`,
          borderRadius: 10,
        }}
      >
        <ColumnHeader
          label="general notes"
          accentColor="var(--neon-green)"
          count={freeNotes.length}
        />
        <div
          style={{
            flex: 1,
            minHeight: 0,
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
            overflowY: 'auto',
          }}
        >
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
              e.preventDefault()
              e.dataTransfer.dropEffect = 'move'
              setOverPhase(phase)
            }}
            onDragLeave={(e) => {
              if (!e.currentTarget.contains(e.relatedTarget))
                setOverPhase((p) => (p === phase ? null : p))
            }}
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
              display: 'flex',
              flexDirection: 'column',
              gap: 8,
              padding: 8,
              background: isOver ? a.glow : 'var(--bg-card)',
              border: `1px ${isOver ? 'dashed' : 'solid'} ${isOver ? a.bar : 'var(--border-subtle)'}`,
              borderRadius: 10,
              transition: 'flex-basis 0.2s ease, background 0.15s ease',
              overflow: 'hidden',
            }}
          >
            {collapsed ? (
              <div
                style={{
                  ...mono,
                  fontSize: 9,
                  color: a.bar,
                  textTransform: 'uppercase',
                  letterSpacing: '0.1em',
                  writingMode: 'vertical-rl',
                  margin: '4px auto 0',
                  opacity: 0.7,
                  whiteSpace: 'nowrap',
                }}
              >
                {phase}
              </div>
            ) : (
              <>
                <ColumnHeader label={phase} accentColor={a.bar} count={inPhase.length} />
                <div
                  style={{
                    flex: 1,
                    minHeight: 0,
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 8,
                    overflowY: 'auto',
                  }}
                >
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
