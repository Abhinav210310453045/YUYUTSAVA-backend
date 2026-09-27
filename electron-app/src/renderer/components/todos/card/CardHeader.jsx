import React, { useRef } from 'react'
import BackButton from '../../layout/BackButton'
import { PinIcon, STATUS_ACCENT } from '../shared'
import ObjectiveComposer from './ObjectiveComposer'

const STATUSES = ['inbox', 'active', 'done', 'archived']

// The card view's top bar: back, the renamable title, progress, pin, status,
// and the "+ Objective", Select, Journey and Tinker actions.
export default function CardHeader({
  cardId,
  card,
  onCardChange,
  title,
  onTitleChange,
  onTitleCommit,
  onTitleRevert,
  patching,
  patch,
  selectMode,
  onToggleSelectMode,
  journeyBusy,
  onJourney,
  thinkOpen,
  onToggleThink,
}) {
  const titleRef = useRef(null)
  const accent = STATUS_ACCENT[card?.status] || STATUS_ACCENT.inbox
  const doneCount = card ? card.objectives.filter((o) => o.phase === 'completed').length : 0

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '14px 24px',
        borderBottom: '1px solid var(--border-subtle)',
        background: 'var(--bg-bar)',
      }}
    >
      <BackButton variant="labelled" label="Board" title="back to board" />

      {card && (
        <>
          {/* Renamable title — commit on Enter or blur, Esc reverts. */}
          <input
            ref={titleRef}
            value={title}
            onChange={(e) => {
              onTitleChange(e.target.value)
            }}
            onBlur={onTitleCommit}
            onKeyDown={(e) => {
              if (e.key === 'Enter') titleRef.current?.blur()
              if (e.key === 'Escape') {
                onTitleRevert()
                titleRef.current?.blur()
              }
            }}
            disabled={patching}
            title="click to rename"
            style={{
              flex: 1,
              minWidth: 0,
              background: 'transparent',
              color: 'var(--text-primary)',
              border: '1px solid transparent',
              borderRadius: 6,
              padding: '5px 10px',
              fontFamily: 'var(--font-mono)',
              fontSize: 13,
              fontWeight: 'var(--fw-semibold)',
              outline: 'none',
            }}
            onFocus={(e) => {
              e.target.style.border = '1px solid var(--border-card)'
              e.target.style.background = 'var(--bg-card)'
            }}
            onBlurCapture={(e) => {
              e.target.style.border = '1px solid transparent'
              e.target.style.background = 'transparent'
            }}
          />

          {doneCount > 0 || card.objectives.length > 0 ? (
            <span
              style={{
                fontFamily: 'var(--font-mono)',
                fontSize: 10,
                color: 'var(--neon-green)',
                background: 'rgba(var(--accent-rgb),0.08)',
                border: '1px solid rgba(var(--accent-rgb),0.2)',
                borderRadius: 10,
                padding: '1px 7px',
                whiteSpace: 'nowrap',
              }}
            >
              {doneCount}/{card.objectives.length}
            </span>
          ) : null}

          <button
            onClick={() => patch({ pinned: !card.pinned })}
            disabled={patching}
            title={card.pinned ? 'unpin' : 'pin to top'}
            style={{
              width: 28,
              height: 28,
              borderRadius: 6,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              background: card.pinned ? 'rgba(250,204,21,0.10)' : 'transparent',
              border: `1px solid ${card.pinned ? 'rgba(250,204,21,0.3)' : 'var(--border-card)'}`,
              cursor: 'pointer',
            }}
          >
            <PinIcon color={card.pinned ? 'var(--text-warning)' : 'var(--text-muted)'} />
          </button>

          <select
            value={card.status}
            onChange={(e) => patch({ status: e.target.value })}
            disabled={patching}
            title="card status"
            style={{
              background: 'var(--bg-card)',
              color: accent.bar,
              border: `1px solid ${accent.border}`,
              borderRadius: 6,
              padding: '5px 8px',
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              cursor: 'pointer',
            }}
          >
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>

          <ObjectiveComposer cardId={cardId} onCardChange={onCardChange} />

          {/* Multi-select of notes/objectives → a reference block seeded
              into the tinker chat. The count button appears once armed. */}
          <button
            onClick={onToggleSelectMode}
            title={
              selectMode ? 'exit selection' : 'select notes/objectives to ask the TinkerAgent about'
            }
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              padding: '5px 12px',
              background: selectMode ? 'rgba(196,181,253,0.16)' : 'transparent',
              color: selectMode ? 'var(--text-lavender)' : 'var(--text-muted)',
              border: `1px solid ${selectMode ? 'rgba(167,139,250,0.4)' : 'var(--border-card)'}`,
              borderRadius: 6,
              cursor: 'pointer',
              whiteSpace: 'nowrap',
            }}
          >
            {selectMode ? '✕ Select' : '☐ Select'}
          </button>

          <button
            onClick={onJourney}
            disabled={journeyBusy}
            title="compile this card's journey — objectives, notes, and timeline — into a document"
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              padding: '5px 12px',
              background: 'rgba(250,204,21,0.08)',
              color: 'var(--text-warning)',
              border: '1px solid rgba(250,204,21,0.3)',
              borderRadius: 6,
              cursor: journeyBusy ? 'default' : 'pointer',
              opacity: journeyBusy ? 0.6 : 1,
              whiteSpace: 'nowrap',
            }}
          >
            {journeyBusy ? '… compiling' : '📜 Journey'}
          </button>

          <button
            onClick={onToggleThink}
            title={
              thinkOpen ? 'close the TinkerAgent chat' : 'think on this card with the TinkerAgent'
            }
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              padding: '5px 12px',
              background: thinkOpen ? 'rgba(120,160,255,0.18)' : 'rgba(120,160,255,0.06)',
              color: 'var(--text-info)',
              border: `1px solid rgba(120,160,255,${thinkOpen ? 0.5 : 0.3})`,
              borderRadius: 6,
              cursor: 'pointer',
              whiteSpace: 'nowrap',
            }}
          >
            {thinkOpen ? '✕ Tinker' : '✦ Tinker'}
          </button>
        </>
      )}
    </div>
  )
}
