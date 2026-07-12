import React from 'react'

// Shared bits for the TODO board (TodosPanel + TodoCardView).

// One accent per card status — the four values of the exchange CardStatus.
// Same shape as SessionRow's ORIGIN_ACCENT (left bar + tinted border + soft
// glow) so board cards read like the rest of the app's card surfaces.
export const STATUS_ACCENT = {
  inbox:    { bar: '#7aa2ff', border: 'rgba(120, 160, 255, 0.34)', glow: 'rgba(120, 160, 255, 0.12)', hover: 'rgba(120, 160, 255, 0.32)' },
  active:   { bar: '#00ff88', border: 'rgba(0, 255, 136, 0.30)', glow: 'rgba(0, 255, 136, 0.10)', hover: 'rgba(0, 255, 136, 0.28)' },
  done:     { bar: '#facc15', border: 'rgba(250, 204, 21, 0.30)', glow: 'rgba(250, 204, 21, 0.10)', hover: 'rgba(250, 204, 21, 0.28)' },
  archived: { bar: '#8888a0', border: 'rgba(136, 136, 160, 0.30)', glow: 'rgba(136, 136, 160, 0.10)', hover: 'rgba(136, 136, 160, 0.28)' },
}

export function humanAge(unixSec) {
  const d = Math.max(0, Date.now() / 1000 - unixSec)
  if (d < 60) return `${Math.floor(d)}s ago`
  if (d < 3600) return `${Math.floor(d / 60)}m ago`
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`
  return `${Math.floor(d / 86400)}d ago`
}

export function TagChips({ tags }) {
  if (!tags || tags.length === 0) return null
  return (
    <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
      {tags.map((t) => (
        <span key={t} style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 9,
          padding: '1px 6px',
          borderRadius: 8,
          background: 'rgba(120, 160, 255, 0.12)',
          color: '#9bb8ff',
          border: '1px solid rgba(120, 160, 255, 0.25)',
        }}>
          {t}
        </span>
      ))}
    </div>
  )
}

// 12px pin glyph for pinned cards.
export function PinIcon({ color = '#facc15' }) {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="12" y1="17" x2="12" y2="22"/>
      <path d="M5 17h14l-2-6V5a2 2 0 0 0-2-2H9a2 2 0 0 0-2 2v6l-2 6z"/>
    </svg>
  )
}
