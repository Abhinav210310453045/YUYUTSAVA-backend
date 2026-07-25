import React from 'react'
import { useViewState } from '../../nav/useViewState'

export default function SettingsSection({ title, children, defaultOpen = true }) {
  // Which sections you had open is part of "where you were" — keep it across
  // tab switches (the panel unmounts) rather than snapping back to defaults.
  const [open, setOpen] = useViewState(`section:${title}`, defaultOpen)

  return (
    <div style={{
      border: '1px solid var(--border-card)',
      borderRadius: 'var(--radius-card)',
    }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          width: '100%',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '10px 16px',
          background: 'var(--bg-elevated)',
          color: 'var(--text-secondary)',
          fontSize: 11,
          fontFamily: 'var(--font-mono)',
          fontWeight: 'var(--fw-semibold)',
          letterSpacing: '0.1em',
          textTransform: 'uppercase',
          borderBottom: open ? '1px solid var(--border-subtle)' : 'none',
          borderRadius: open ? 'var(--radius-card) var(--radius-card) 0 0' : 'var(--radius-card)',
          cursor: 'pointer',
          transition: 'background 0.15s',
        }}
      >
        {title}
        <span style={{ fontSize: 10, color: 'var(--text-muted)', transform: open ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 0.2s' }}>›</span>
      </button>

      {open && (
        <div style={{
          background: 'var(--bg-card)',
          padding: '16px',
          display: 'flex',
          flexDirection: 'column',
          gap: 14,
          borderRadius: '0 0 var(--radius-card) var(--radius-card)',
        }}>
          {children}
        </div>
      )}
    </div>
  )
}
