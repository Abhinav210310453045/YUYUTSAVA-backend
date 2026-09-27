import React, { useState } from 'react'
import { addTodoObjective } from '../../../api/client'

// The header's "+ Objective" popover — new objectives start in thinking.
export default function ObjectiveComposer({ cardId, onCardChange }) {
  const [objComposerOpen, setObjComposerOpen] = useState(false)
  const [newObjective, setNewObjective] = useState('')
  const [addingObjective, setAddingObjective] = useState(false)

  const onAddObjective = async () => {
    const t = newObjective.trim()
    if (!t || addingObjective) return
    setAddingObjective(true)
    try {
      const obj = await addTodoObjective(cardId, t)
      onCardChange((c) => (c ? { ...c, objectives: [...c.objectives, obj] } : c))
      setNewObjective('')
      setObjComposerOpen(false)
      // New objectives land in thinking — bring that column into view.
      requestAnimationFrame(() => {
        document
          .getElementById('yy-phase-thinking')
          ?.scrollIntoView({ behavior: 'smooth', inline: 'nearest', block: 'nearest' })
      })
    } catch (e) {
      alert(`Add objective failed: ${e.message}`)
    } finally {
      setAddingObjective(false)
    }
  }

  return objComposerOpen ? (
    <input
      autoFocus
      value={newObjective}
      onChange={(e) => setNewObjective(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onAddObjective()
        if (e.key === 'Escape') {
          setObjComposerOpen(false)
          setNewObjective('')
        }
      }}
      onBlur={() => {
        if (!newObjective.trim()) setObjComposerOpen(false)
      }}
      placeholder="objective title… (Enter)"
      disabled={addingObjective}
      style={{
        width: 220,
        background: 'var(--bg-card)',
        color: 'var(--text-primary)',
        border: '1px solid rgba(167,139,250,0.4)',
        borderRadius: 6,
        padding: '5px 10px',
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        outline: 'none',
      }}
    />
  ) : (
    <button
      onClick={() => setObjComposerOpen(true)}
      title="add an objective (a small, checkable step) — starts in thinking"
      style={{
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        padding: '5px 12px',
        background: 'rgba(167,139,250,0.10)',
        color: 'var(--text-lavender)',
        border: '1px solid rgba(167,139,250,0.3)',
        borderRadius: 6,
        cursor: 'pointer',
        whiteSpace: 'nowrap',
      }}
    >
      + Objective
    </button>
  )
}
