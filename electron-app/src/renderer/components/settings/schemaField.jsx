import React from 'react'
import SettingsField from './SettingsField'
import WakeWordsEditor from './WakeWordsEditor'

// One schema variable as a form control. A function rather than a component:
// it returns null for a variable whose depends_key hides it, and the panel
// drops a group whose fields all come back null.
export function renderField(v, settings, onChange) {
  // Conditional visibility (e.g. provider-specific keys, postgres-only fields).
  if (v.depends_key && (settings[v.depends_key] ?? '') !== v.depends_value) return null
  // Wake words get a dedicated list editor (add/remove + presets) instead of a
  // raw comma-separated text box; it hot-applies via the voice events source.
  if (v.key === 'WAKE_WORDS') {
    return (
      <div key={v.key} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <label
          style={{
            fontSize: 11,
            color: 'var(--text-secondary)',
            fontFamily: 'var(--font-mono)',
            letterSpacing: '0.04em',
          }}
        >
          {v.label}
          <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontSize: 10 }}>{v.key}</span>
        </label>
        <WakeWordsEditor
          value={settings.WAKE_WORDS}
          threshold={settings.WAKE_THRESHOLD}
          onChange={onChange}
        />
      </div>
    )
  }
  const options =
    v.options && v.options.length
      ? v.options.map((o) => ({ value: o, label: o === '' ? '(default)' : o }))
      : undefined
  return (
    <SettingsField
      key={v.key}
      label={v.label}
      envKey={v.key}
      type={v.type || 'text'}
      value={settings[v.key]}
      onChange={onChange}
      placeholder={v.placeholder || v.default || ''}
      options={options}
    />
  )
}
