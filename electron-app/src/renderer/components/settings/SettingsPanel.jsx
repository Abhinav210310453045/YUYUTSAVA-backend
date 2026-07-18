import React, { useState, useEffect, useMemo } from 'react'
import SettingsSection from './SettingsSection'
import SettingsField from './SettingsField'
import WatchedDirsEditor from './WatchedDirsEditor'
import WakeWordsEditor from './WakeWordsEditor'
import { getConfigSchema } from '../../api/client'

function DaemonBtn({ label, color, borderColor, bg, disabled, onClick }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: '5px 14px',
        borderRadius: 'var(--radius-btn)',
        fontSize: 10,
        fontFamily: 'var(--font-mono)',
        fontWeight: 700,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        border: `1px solid ${borderColor}`,
        background: bg,
        color,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
        transition: 'all 0.15s',
        flexShrink: 0,
      }}
    >
      {disabled ? '...' : label}
    </button>
  )
}

// Minimal fallback so the form is usable before the daemon's first boot
// (the schema endpoint is unreachable then). Mirrors the daemon's Core group.
const FALLBACK_SCHEMA = {
  groups: [
    {
      name: 'LLM Provider',
      vars: [
        { key: 'LLM_PROVIDER', label: 'Provider', type: 'select', default: 'groq',
          options: ['groq', 'openrouter', 'anthropic', 'ollama'], reload_class: 'restart_resume' },
        { key: 'GROQ_API_KEY', label: 'Groq API key', type: 'password', secret: true,
          placeholder: 'gsk_...', reload_class: 'restart_resume',
          depends_key: 'LLM_PROVIDER', depends_value: 'groq' },
        { key: 'GROQ_MODEL', label: 'Groq model', type: 'text',
          placeholder: 'llama-3.3-70b-versatile', reload_class: 'restart_resume',
          depends_key: 'LLM_PROVIDER', depends_value: 'groq' },
      ],
    },
    {
      name: 'Daemon',
      vars: [
        { key: 'YUYUTSAVA_DAEMON_PORT', label: 'Port', type: 'number', default: '7654',
          placeholder: '7654', reload_class: 'restart_no_resume' },
      ],
    },
  ],
}

export default function SettingsPanel() {
  const [settings, setSettings] = useState({})
  const [initial, setInitial] = useState({})
  const [schema, setSchema] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saved, setSaved] = useState(false)
  const [daemonStatus, setDaemonStatus] = useState(null)
  const [daemonBusy, setDaemonBusy] = useState(false)
  const [reloadPrompt, setReloadPrompt] = useState(null)  // { keys: [...], noResume: bool }

  useEffect(() => {
    Promise.all([
      window.electronAPI?.getSettings() || Promise.resolve({}),
      window.electronAPI?.getDaemonStatus() || Promise.resolve(null),
      getConfigSchema().catch(() => FALLBACK_SCHEMA),
    ]).then(([s, status, sch]) => {
      setSettings(s || {})
      setInitial(s || {})
      setDaemonStatus(status)
      setSchema(sch && Array.isArray(sch.groups) && sch.groups.length ? sch : FALLBACK_SCHEMA)
      setLoading(false)
    })

    // Poll status so externally-started/stopped daemons reflect in the UI.
    const id = setInterval(async () => {
      const status = await window.electronAPI?.getDaemonStatus()
      if (status) setDaemonStatus(status)
    }, 3000)
    return () => clearInterval(id)
  }, [])

  // key → reload_class / label lookups built from the active schema.
  const { reloadClassOf, labelOf } = useMemo(() => {
    const rc = new Map()
    const lb = new Map()
    for (const g of schema?.groups || []) {
      for (const v of g.vars) {
        rc.set(v.key, v.reload_class || 'restart_resume')
        lb.set(v.key, v.label || v.key)
      }
    }
    return {
      reloadClassOf: (k) => rc.get(k) || 'restart_resume',
      labelOf: (k) => lb.get(k) || k,
    }
  }, [schema])

  async function refreshStatus() {
    const status = await window.electronAPI?.getDaemonStatus()
    setDaemonStatus(status)
  }

  function onChange(key, val) {
    setSettings(prev => ({ ...prev, [key]: val }))
  }

  async function save() {
    await window.electronAPI?.saveSettings(settings)
    const changed = Object.keys(settings).filter(k => (settings[k] ?? '') !== (initial[k] ?? ''))
    const restartKeys = changed.filter(k => reloadClassOf(k) !== 'hot')
    const noResume = restartKeys.some(k => reloadClassOf(k) === 'restart_no_resume')
    setInitial({ ...settings })  // new baseline
    await refreshStatus()
    if (restartKeys.length && daemonStatus?.running) {
      setReloadPrompt({ keys: restartKeys, noResume })
    } else {
      setSaved(true)
      setTimeout(() => setSaved(false), 4000)
    }
  }

  async function applyReload() {
    setReloadPrompt(null)
    setDaemonBusy(true)
    await window.electronAPI?.restartDaemon()
    setSaved(true)
    setTimeout(() => setSaved(false), 4000)
    setTimeout(async () => { await refreshStatus(); setDaemonBusy(false) }, 2500)
  }

  async function startDaemon() {
    setDaemonBusy(true)
    await window.electronAPI?.startDaemon()
    setTimeout(async () => { await refreshStatus(); setDaemonBusy(false) }, 1500)
  }

  async function stopDaemon() {
    setDaemonBusy(true)
    await window.electronAPI?.stopDaemon()
    setTimeout(async () => { await refreshStatus(); setDaemonBusy(false) }, 1500)
  }

  async function restartDaemon() {
    setDaemonBusy(true)
    await window.electronAPI?.restartDaemon()
    setTimeout(async () => { await refreshStatus(); setDaemonBusy(false) }, 2000)
  }

  function renderField(v) {
    // Conditional visibility (e.g. provider-specific keys, postgres-only fields).
    if (v.depends_key && (settings[v.depends_key] ?? '') !== v.depends_value) return null
    // Wake words get a dedicated list editor (add/remove + presets) instead of a
    // raw comma-separated text box; it hot-applies via the voice events source.
    if (v.key === 'WAKE_WORDS') {
      return (
        <div key={v.key} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <label style={{ fontSize: 11, color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)', letterSpacing: '0.04em' }}>
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
    const options = (v.options && v.options.length)
      ? v.options.map(o => ({ value: o, label: o === '' ? '(default)' : o }))
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

  if (loading) {
    return (
      <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
        loading settings...
      </div>
    )
  }

  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
        <h2 style={{ fontSize: 13, fontWeight: 'var(--fw-semibold)', fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.1em' }}>
          Settings
        </h2>
        <button
          onClick={save}
          style={{
            padding: '6px 18px',
            borderRadius: 'var(--radius-btn)',
            fontSize: 11,
            fontFamily: 'var(--font-mono)',
            fontWeight: 'var(--fw-semibold)',
            letterSpacing: '0.05em',
            textTransform: 'uppercase',
            border: '1px solid',
            cursor: 'pointer',
            color: saved ? 'var(--neon-green)' : 'var(--text-primary)',
            borderColor: saved ? 'rgba(var(--accent-rgb),0.4)' : 'var(--border-card)',
            background: saved ? 'rgba(var(--accent-rgb),0.08)' : 'var(--bg-elevated)',
            transition: 'all 0.2s',
          }}
        >
          {saved ? '✓ Saved' : 'Save'}
        </button>
      </div>

      {saved && (
        <div style={{
          background: 'rgba(var(--accent-rgb),0.06)',
          border: '1px solid rgba(var(--accent-rgb),0.2)',
          borderRadius: 'var(--radius-card)',
          padding: '9px 14px',
          fontSize: 11,
          color: 'var(--neon-green)',
          fontFamily: 'var(--font-mono)',
        }}>
          ✓ Settings saved.
        </div>
      )}

      {/* Reload prompt: changed vars need a daemon restart to take effect. */}
      {reloadPrompt && (
        <div style={{
          background: 'rgba(251,191,36,0.07)',
          border: '1px solid rgba(251,191,36,0.3)',
          borderRadius: 'var(--radius-card)',
          padding: '12px 14px',
          fontSize: 11,
          color: 'var(--neon-amber)',
          fontFamily: 'var(--font-mono)',
          display: 'flex', flexDirection: 'column', gap: 8,
        }}>
          <div>
            Restart the daemon to apply changes to{' '}
            <strong>{reloadPrompt.keys.map(labelOf).join(', ')}</strong>.
          </div>
          <div style={{ color: reloadPrompt.noResume ? 'var(--neon-red)' : 'var(--text-muted)' }}>
            {reloadPrompt.noResume
              ? '⚠ A running task will restart from the beginning (storage/port change).'
              : 'A running task will resume from its last checkpoint.'}
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <DaemonBtn
              label="Restart now"
              color="var(--neon-amber)"
              borderColor="rgba(251,191,36,0.3)"
              bg="rgba(251,191,36,0.06)"
              disabled={daemonBusy}
              onClick={applyReload}
            />
            <DaemonBtn
              label="Later"
              color="var(--text-muted)"
              borderColor="var(--border-card)"
              bg="var(--bg-elevated)"
              disabled={daemonBusy}
              onClick={() => setReloadPrompt(null)}
            />
          </div>
        </div>
      )}

      {/* Daemon status + controls */}
      <div style={{
        background: 'var(--bg-elevated)',
        border: `1px solid ${daemonStatus?.running ? 'rgba(var(--accent-rgb),0.15)' : 'rgba(255,51,102,0.15)'}`,
        borderRadius: 'var(--radius-card)',
        padding: '10px 14px',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
      }}>
        {/* Status indicator */}
        <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontFamily: 'var(--font-mono)', fontSize: 11, flex: 1 }}>
          <span style={{
            width: 7, height: 7, borderRadius: '50%', flexShrink: 0,
            background: daemonStatus?.running ? 'var(--neon-green)' : 'var(--neon-red)',
            boxShadow: daemonStatus?.running ? 'var(--glow-green)' : 'var(--glow-red)',
            animation: daemonStatus?.running ? 'neon-pulse 2s ease-in-out infinite' : 'none',
          }} />
          <span style={{ color: daemonStatus?.running ? 'var(--neon-green)' : 'var(--neon-red)', fontWeight: 'var(--fw-semibold)' }}>
            {daemonStatus?.running ? 'Running' : 'Stopped'}
          </span>
          <span style={{ color: 'var(--text-dim)' }}>·</span>
          <span style={{ color: 'var(--text-muted)' }}>port {daemonStatus?.port ?? '7654'}</span>
          {daemonStatus?.managed && (
            <>
              <span style={{ color: 'var(--text-dim)' }}>·</span>
              <span style={{ color: 'var(--text-muted)' }}>managed</span>
            </>
          )}
        </span>

        {/* Action buttons */}
        {daemonStatus?.running ? (
          <>
            <DaemonBtn
              label="Restart"
              color="var(--neon-amber)"
              borderColor="rgba(251,191,36,0.3)"
              bg="rgba(251,191,36,0.06)"
              disabled={daemonBusy}
              onClick={restartDaemon}
            />
            <DaemonBtn
              label="Stop"
              color="var(--neon-red)"
              borderColor="rgba(255,51,102,0.3)"
              bg="rgba(255,51,102,0.06)"
              disabled={daemonBusy}
              onClick={stopDaemon}
            />
          </>
        ) : (
          <DaemonBtn
            label="Start"
            color="var(--neon-green)"
            borderColor="rgba(var(--accent-rgb),0.3)"
            bg="rgba(var(--accent-rgb),0.06)"
            disabled={daemonBusy}
            onClick={startDaemon}
          />
        )}
      </div>

      <SettingsSection title="Watched Directories" defaultOpen={true}>
        <WatchedDirsEditor />
      </SettingsSection>

      {/* Schema-driven config groups (served by the daemon). */}
      {(schema?.groups || []).map((g, idx) => {
        const fields = g.vars.map(renderField).filter(Boolean)
        if (!fields.length) return null
        return (
          <SettingsSection key={g.name} title={g.name} defaultOpen={idx < 2}>
            {fields}
          </SettingsSection>
        )
      })}
    </div>
  )
}
