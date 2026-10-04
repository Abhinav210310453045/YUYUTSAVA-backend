import React from 'react'

// Small pieces the models screens share (setup wizard, Settings → Models, the
// composer's model chip), drawn from the theme's variables so every theme fits.

export function Button({ children, onClick, disabled, primary, danger, title, small }) {
  const colour = danger
    ? 'var(--neon-red)'
    : primary
      ? 'var(--neon-green)'
      : 'var(--text-secondary)'
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="tap-pop"
      style={{
        padding: small ? '3px 10px' : '7px 16px',
        borderRadius: 'var(--radius-btn)',
        border: `1px solid ${primary ? 'rgba(var(--accent-rgb), 0.45)' : 'var(--border-card)'}`,
        background: primary ? 'rgba(var(--accent-rgb), 0.12)' : 'transparent',
        color: colour,
        fontFamily: 'var(--font-ui)',
        fontSize: small ? 11 : 13,
        fontWeight: primary ? 'var(--fw-semibold)' : 400,
        cursor: disabled ? 'default' : 'pointer',
        opacity: disabled ? 0.45 : 1,
      }}
    >
      {children}
    </button>
  )
}

export function TextInput({ value, onChange, placeholder, secret, autoFocus, style }) {
  return (
    <input
      type={secret ? 'password' : 'text'}
      value={value}
      autoFocus={autoFocus}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      spellCheck={false}
      style={{
        width: '100%',
        padding: '7px 10px',
        borderRadius: 'var(--radius-btn)',
        border: '1px solid var(--border-card)',
        background: 'var(--bg-elevated)',
        color: 'var(--text-primary)',
        fontFamily: 'var(--font-mono)',
        fontSize: 12,
        boxSizing: 'border-box',
        ...style,
      }}
    />
  )
}

export function Labelled({ label, hint, children }) {
  return (
    <label style={{ display: 'block', marginBottom: 12 }}>
      <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 5 }}>
        {label}
        {hint && <span style={{ color: 'var(--text-dim)', marginLeft: 6 }}>{hint}</span>}
      </div>
      {children}
    </label>
  )
}

// One cell, as the CLI draws them: ready, needs a quota or access, not served here, unknown.
const MARKS = {
  available: ['✓', 'var(--neon-green)', 'ready'],
  needs_quota: ['⊘', 'var(--neon-amber)', 'needs a quota increase'],
  needs_access: ['⊘', 'var(--neon-amber)', 'needs access'],
  not_in_region: ['⌖', 'var(--neon-amber)', 'not served in this region'],
  unknown: ['?', 'var(--text-dim)', 'unknown'],
}

export function Availability({ value }) {
  const [mark, colour, label] = MARKS[value] || MARKS.unknown
  return (
    <span
      title={label}
      style={{ color: colour, fontFamily: 'var(--font-mono)', width: 14, display: 'inline-block' }}
    >
      {mark}
    </span>
  )
}

export function Panel({ children, style }) {
  return (
    <div
      style={{
        border: '1px solid var(--border-card)',
        borderRadius: 'var(--radius-card)',
        background: 'var(--bg-card)',
        padding: '12px 14px',
        marginBottom: 10,
        ...style,
      }}
    >
      {children}
    </div>
  )
}

export function Problem({ children }) {
  return (
    <div style={{ color: 'var(--neon-red)', fontSize: 12, whiteSpace: 'pre-wrap' }}>{children}</div>
  )
}

export function Muted({ children, style }) {
  return <div style={{ color: 'var(--text-muted)', fontSize: 12, ...style }}>{children}</div>
}
