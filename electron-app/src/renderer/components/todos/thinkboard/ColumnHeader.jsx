import React from 'react'
import { mono } from './common'

// A board column's dot, label and count.
export default function ColumnHeader({ label, accentColor, count }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
      <span
        style={{ width: 6, height: 6, borderRadius: '50%', background: accentColor, flexShrink: 0 }}
      />
      <span
        style={{
          ...mono,
          fontSize: 10,
          color: accentColor,
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
        }}
      >
        {label}
      </span>
      {count > 0 && <span style={{ ...mono, fontSize: 9, color: 'var(--text-dim)' }}>{count}</span>}
    </div>
  )
}
