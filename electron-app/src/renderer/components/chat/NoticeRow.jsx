import React, { useState } from 'react'
import { alpha } from '../../lib/color'

// A standalone row for something the system needs to say — the model returned
// nothing, a history was repaired. Not a bubble: it is not something the agent
// said, and styling it as speech would misattribute it.
//
// `actions` are what the user can do about it right now ([{ id, label }], see
// conversations/noticeActions.js), offered as buttons; `onAction(id)` does
// them. The chat, the TODO card's tinker chat and the voice screen all render
// notices with this row, each mapping the actions to its own New / Retry.

const TONES = {
  error: { fg: 'var(--neon-red)', bd: 'var(--border-red)', mark: '✗' },
  warning: { fg: 'var(--neon-amber)', bd: 'var(--border-amber)', mark: '⚠' },
  info: { fg: 'var(--text-muted)', bd: 'var(--border-subtle)', mark: '·' },
}

const GLYPHS = { retry: '↻', new_chat: '＋' }

function ActionButton({ action, color, onAction }) {
  const [hover, setHover] = useState(false)
  return (
    <button
      onClick={() => onAction(action.id)}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      data-notice-action={action.id}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        fontFamily: 'var(--font-mono)',
        fontSize: 10,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        fontWeight: 'var(--fw-semibold)',
        color: hover ? 'var(--bg-deep)' : color,
        background: hover ? color : 'transparent',
        border: `1px solid ${color}`,
        borderRadius: 6,
        padding: '3px 9px',
        cursor: 'pointer',
        transition: 'background 0.15s ease, color 0.15s ease, box-shadow 0.15s ease',
        boxShadow: hover ? `0 0 10px ${alpha(color, 40)}` : 'none',
      }}
    >
      <span style={{ fontSize: 11, lineHeight: 1 }}>{GLYPHS[action.id] || '›'}</span>
      <span>{action.label}</span>
    </button>
  )
}

export default function NoticeRow({ m, actions = [], onAction = null }) {
  const tone = TONES[m.level] || TONES.info
  const offered = onAction ? actions : []
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        flexShrink: 0,
        padding: '8px 12px',
        borderRadius: 'var(--radius-card)',
        border: `1px solid ${tone.bd}`,
        background: 'rgba(255,255,255,0.03)',
        fontSize: 12,
        lineHeight: 1.6,
        color: tone.fg,
      }}
    >
      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
        <span style={{ fontFamily: 'var(--font-mono)' }}>{tone.mark}</span>
        <span style={{ color: 'var(--text-secondary)' }}>{m.text}</span>
      </div>
      {offered.length > 0 && (
        <div style={{ display: 'flex', gap: 8, paddingLeft: 18 }}>
          {offered.map((a) => (
            <ActionButton key={a.id} action={a} color={tone.fg} onAction={onAction} />
          ))}
        </div>
      )}
    </div>
  )
}
