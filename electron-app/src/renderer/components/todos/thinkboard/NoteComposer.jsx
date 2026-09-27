import React, { useState } from 'react'
import { addTodoNote } from '../../../api/client'
import { useDictation } from '../../../hooks/useDictation'
import { mono } from './common'

// The add-a-note box, shared by General Notes and the objective cards.

export default function NoteComposer({
  cardId,
  objective = null,
  onCardChange,
  withMic = false,
  compact = false,
}) {
  const [draft, setDraft] = useState('')
  const [adding, setAdding] = useState(false)

  // STT dictation into the draft (never auto-submitted) — the mic renders
  // only in the General Notes composer, keeping one recorder per card view.
  const dictation = useDictation({
    onText: (text) =>
      setDraft((cur) => {
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
      const note = await addTodoNote(
        cardId,
        body,
        'user',
        objective
          ? {
              objectiveId: objective.objective_id,
              phase: objective.phase,
            }
          : {},
      )
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
        placeholder={
          objective
            ? 'note on this objective… (⌘/Ctrl+Enter)'
            : 'add a note… (⌘/Ctrl+Enter to save)'
        }
        rows={compact ? 2 : 3}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) onAdd()
        }}
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
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        {withMic && (
          <button
            onClick={dictation.toggle}
            disabled={dictation.finishing}
            title={dictation.dictating ? 'stop dictating' : 'dictate a note (speech-to-text)'}
            style={{
              ...mono,
              fontSize: 10,
              padding: '4px 10px',
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
            ...mono,
            fontSize: 10,
            padding: '4px 12px',
            background: 'rgba(var(--accent-rgb),0.08)',
            color: 'var(--neon-green)',
            border: '1px solid rgba(var(--accent-rgb),0.25)',
            borderRadius: 6,
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
