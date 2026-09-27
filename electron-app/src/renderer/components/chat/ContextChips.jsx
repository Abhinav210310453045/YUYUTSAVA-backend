import React from 'react'

// What the next message will be scoped to: the board selection as removable
// pills above the composer. `chips` = [{ key, label, title?, accent? }].
export default function ContextChips({ chips, onRemoveChip, onClearChips }) {
  if (!chips || chips.length === 0) return null
  return (
    <div
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'center',
        gap: 6,
        padding: '8px 24px 0',
        position: 'relative',
        zIndex: 1,
      }}
    >
      <span
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 9,
          color: 'var(--text-dim)',
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
        }}
      >
        context
      </span>
      {chips.map((chip) => (
        <span
          key={chip.key}
          title={chip.title || chip.label}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 5,
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            padding: '2px 8px',
            borderRadius: 10,
            background: chip.accent?.glow || 'rgba(120,160,255,0.10)',
            color: chip.accent?.bar || 'var(--text-info)',
            border: `1px solid ${chip.accent?.border || 'rgba(120,160,255,0.3)'}`,
            maxWidth: 220,
          }}
        >
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {chip.label}
          </span>
          <button
            onClick={() => onRemoveChip?.(chip.key)}
            title="remove from context"
            style={{
              background: 'none',
              border: 'none',
              color: 'inherit',
              cursor: 'pointer',
              padding: 0,
              fontSize: 10,
              lineHeight: 1,
            }}
          >
            ✕
          </button>
        </span>
      ))}
      {chips.length > 1 && (
        <button
          onClick={() => onClearChips?.()}
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 9,
            padding: '2px 8px',
            background: 'transparent',
            color: 'var(--text-muted)',
            border: '1px solid var(--border-card)',
            borderRadius: 10,
            cursor: 'pointer',
          }}
        >
          clear all
        </button>
      )}
    </div>
  )
}
