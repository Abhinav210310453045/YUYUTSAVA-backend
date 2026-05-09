import React from 'react'

export default function SettingsField({ label, envKey, type = 'text', value, onChange, placeholder, options }) {
  const inputStyle = {
    width: '100%',
    fontFamily: type === 'password' || type === 'text' ? 'var(--font-mono)' : 'var(--font-ui)',
    fontSize: 12,
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <label style={{
        fontSize: 11,
        color: 'var(--text-secondary)',
        fontFamily: 'var(--font-mono)',
        letterSpacing: '0.04em',
      }}>
        {label}
        <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontSize: 10 }}>
          {envKey}
        </span>
      </label>

      {type === 'select' && options ? (
        <select value={value || ''} onChange={e => onChange(envKey, e.target.value)} style={inputStyle}>
          <option value="">— select —</option>
          {options.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      ) : type === 'toggle' ? (
        <div
          onClick={() => onChange(envKey, value === 'true' ? 'false' : 'true')}
          style={{
            width: 40, height: 22, borderRadius: 11,
            background: value === 'true' ? 'rgba(0,255,136,0.3)' : 'var(--bg-elevated)',
            border: '1px solid',
            borderColor: value === 'true' ? 'var(--neon-green)' : 'var(--border-card)',
            position: 'relative',
            cursor: 'pointer',
            transition: 'all 0.2s',
            boxShadow: value === 'true' ? 'var(--glow-green)' : 'none',
          }}
        >
          <div style={{
            position: 'absolute',
            top: 3, left: value === 'true' ? 20 : 3,
            width: 14, height: 14, borderRadius: '50%',
            background: value === 'true' ? 'var(--neon-green)' : 'var(--text-muted)',
            transition: 'all 0.2s',
          }} />
        </div>
      ) : (
        <input
          type={type}
          value={value || ''}
          onChange={e => onChange(envKey, e.target.value)}
          placeholder={placeholder}
          style={inputStyle}
          autoComplete="off"
          spellCheck={false}
        />
      )}
    </div>
  )
}
