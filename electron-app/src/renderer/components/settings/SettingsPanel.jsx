import React, { useState, useEffect, useMemo, useRef } from 'react'
import SettingsSection from './SettingsSection'
import WatchedDirsEditor from './WatchedDirsEditor'
import { VoiceModeSettings, SubagentSettings } from './RuntimeToggles'
import UsageSettings from './UsageSettings'
import { DaemonStatusBar, ReloadPrompt } from './DaemonControls'
import { FALLBACK_SCHEMA } from './fallbackSchema'
import { renderField } from './schemaField'
import { getConfigSchema } from '../../api/client'
import { useViewState, useScrollRestore } from '../../nav/useViewState'

export default function SettingsPanel() {
  // The edit buffer outlives navigation: stepping out to check something and
  // coming back must not silently discard half-made changes.
  const [settings, setSettings] = useViewState('settings', {})
  const [initial, setInitial] = useViewState('initial', {})
  const [schema, setSchema] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saved, setSaved] = useState(false)
  const [daemonStatus, setDaemonStatus] = useState(null)
  const [daemonBusy, setDaemonBusy] = useState(false)
  const [reloadPrompt, setReloadPrompt] = useState(null) // { keys: [...], noResume: bool }
  const scrollRef = useScrollRestore(!loading)

  // Any field the user has touched but not saved. Read through a ref by the
  // load effect, which must refresh the baseline without stomping the edits.
  const dirty = useMemo(
    () =>
      Object.keys({ ...initial, ...settings }).some(
        (k) => (settings[k] ?? '') !== (initial[k] ?? ''),
      ),
    [settings, initial],
  )
  const dirtyRef = useRef(dirty)
  dirtyRef.current = dirty

  useEffect(() => {
    Promise.all([
      window.electronAPI?.getSettings() || Promise.resolve({}),
      window.electronAPI?.getDaemonStatus() || Promise.resolve(null),
      getConfigSchema().catch(() => FALLBACK_SCHEMA),
    ]).then(([s, status, sch]) => {
      if (!dirtyRef.current) setSettings(s || {})
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
    setSettings((prev) => ({ ...prev, [key]: val }))
  }

  async function save() {
    await window.electronAPI?.saveSettings(settings)
    const changed = Object.keys(settings).filter((k) => (settings[k] ?? '') !== (initial[k] ?? ''))
    const restartKeys = changed.filter((k) => reloadClassOf(k) !== 'hot')
    const noResume = restartKeys.some((k) => reloadClassOf(k) === 'restart_no_resume')
    setInitial({ ...settings }) // new baseline
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
    setTimeout(async () => {
      await refreshStatus()
      setDaemonBusy(false)
    }, 2500)
  }

  async function startDaemon() {
    setDaemonBusy(true)
    await window.electronAPI?.startDaemon()
    setTimeout(async () => {
      await refreshStatus()
      setDaemonBusy(false)
    }, 1500)
  }

  async function stopDaemon() {
    setDaemonBusy(true)
    await window.electronAPI?.stopDaemon()
    setTimeout(async () => {
      await refreshStatus()
      setDaemonBusy(false)
    }, 1500)
  }

  async function restartDaemon() {
    setDaemonBusy(true)
    await window.electronAPI?.restartDaemon()
    setTimeout(async () => {
      await refreshStatus()
      setDaemonBusy(false)
    }, 2000)
  }

  if (loading) {
    return (
      <div
        style={{
          flex: 1,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'var(--text-muted)',
          fontFamily: 'var(--font-mono)',
          fontSize: 12,
        }}
      >
        loading settings...
      </div>
    )
  }

  return (
    <div
      ref={scrollRef}
      style={{
        flex: 1,
        overflowY: 'auto',
        padding: '20px 24px',
        display: 'flex',
        flexDirection: 'column',
        gap: 16,
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 4,
        }}
      >
        <h2
          style={{
            fontSize: 13,
            fontWeight: 'var(--fw-semibold)',
            fontFamily: 'var(--font-mono)',
            color: 'var(--text-secondary)',
            textTransform: 'uppercase',
            letterSpacing: '0.1em',
          }}
        >
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
        <div
          style={{
            background: 'rgba(var(--accent-rgb),0.06)',
            border: '1px solid rgba(var(--accent-rgb),0.2)',
            borderRadius: 'var(--radius-card)',
            padding: '9px 14px',
            fontSize: 11,
            color: 'var(--neon-green)',
            fontFamily: 'var(--font-mono)',
          }}
        >
          ✓ Settings saved.
        </div>
      )}

      {reloadPrompt && (
        <ReloadPrompt
          reloadPrompt={reloadPrompt}
          labelOf={labelOf}
          daemonBusy={daemonBusy}
          onApply={applyReload}
          onLater={() => setReloadPrompt(null)}
        />
      )}

      <DaemonStatusBar
        daemonStatus={daemonStatus}
        daemonBusy={daemonBusy}
        onStart={startDaemon}
        onStop={stopDaemon}
        onRestart={restartDaemon}
      />

      {/* Hot toggles — applied immediately, no Save, no restart. */}
      <SettingsSection title="Voice Mode" defaultOpen={true}>
        <VoiceModeSettings />
      </SettingsSection>

      <SettingsSection title="Subagents" defaultOpen={false}>
        <SubagentSettings />
      </SettingsSection>

      <SettingsSection title="Watched Directories" defaultOpen={true}>
        <WatchedDirsEditor />
      </SettingsSection>

      {/* Token/cost analytics across every session. Closed by default — it
          fetches on open and is a report, not a setting. */}
      <SettingsSection title="Usage" defaultOpen={false}>
        <UsageSettings />
      </SettingsSection>

      {/* Schema-driven config groups (served by the daemon). */}
      {(schema?.groups || []).map((g, idx) => {
        const fields = g.vars.map((x) => renderField(x, settings, onChange)).filter(Boolean)
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
