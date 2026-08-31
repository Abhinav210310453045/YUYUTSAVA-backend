import React, { useEffect, useState } from 'react'
import { useRuntimeSettings } from '../../hooks/useRuntimeSettings'
import { getSubagentRoster } from '../../api/client'

// The hot toggles — voice mode and the dedicated subagents. Unlike the rest of
// this panel (env vars written to the daemon .env, most of them restart-class)
// these apply immediately and are owned by the daemon, so the CLI and the voice
// overlay see the same state. There is no Save button on purpose: flipping the
// switch IS the change.

function Switch({ on, onClick, disabled }) {
  return (
    <div
      onClick={disabled ? undefined : onClick}
      role="switch"
      aria-checked={on}
      style={{
        width: 40, height: 22, borderRadius: 11, flexShrink: 0,
        background: on ? 'rgba(var(--accent-rgb),0.3)' : 'var(--bg-elevated)',
        border: '1px solid',
        borderColor: on ? 'var(--neon-green)' : 'var(--border-card)',
        position: 'relative',
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.45 : 1,
        transition: 'all 0.2s',
        boxShadow: on ? 'var(--glow-green)' : 'none',
      }}
    >
      <div style={{
        position: 'absolute', top: 3, left: on ? 20 : 3,
        width: 14, height: 14, borderRadius: '50%',
        background: on ? 'var(--neon-green)' : 'var(--text-muted)',
        transition: 'all 0.2s',
      }} />
    </div>
  )
}

function ToggleRow({ title, hint, on, onToggle, disabled }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12,
      padding: '8px 0',
    }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontSize: 12, color: 'var(--text-secondary)',
          fontFamily: 'var(--font-mono)', letterSpacing: '0.04em',
        }}>{title}</div>
        {hint && (
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>{hint}</div>
        )}
      </div>
      <Switch on={on} onClick={onToggle} disabled={disabled} />
    </div>
  )
}

export function VoiceModeSettings() {
  const { voice, saving, setVoice } = useRuntimeSettings()
  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      <ToggleRow
        title="Wake word"
        hint="Listen for the wake word in the background. Off releases the microphone entirely — the daemon stops the listener, and the hotkey stops summoning the overlay."
        on={voice.wake_enabled !== false}
        disabled={saving}
        onToggle={() => setVoice({ wake_enabled: voice.wake_enabled === false }).catch(() => {})}
      />
      <ToggleRow
        title="Spoken replies"
        hint="Read answers aloud on voice turns. Off keeps voice input working — you talk, it answers in text."
        on={voice.tts_enabled !== false}
        disabled={saving}
        onToggle={() => setVoice({ tts_enabled: voice.tts_enabled === false }).catch(() => {})}
      />
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>
        The mic button in the Voice panel always works, whatever these say.
      </div>
    </div>
  )
}

export function SubagentSettings() {
  const { disabledSubagents, saving, setSubagentEnabled } = useRuntimeSettings()
  const [roster, setRoster] = useState(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    let alive = true
    getSubagentRoster()
      .then((r) => { if (alive) setRoster(r?.subagents || []) })
      .catch(() => { if (alive) setError(true) })
    return () => { alive = false }
  }, [])

  if (error) {
    return (
      <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
        Could not read the subagent roster — is the daemon running?
      </div>
    )
  }
  if (roster === null) {
    return <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Loading…</div>
  }
  if (!roster.length) {
    return (
      <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
        This daemon registered no subagents.
      </div>
    )
  }

  // The roster is served by the daemon from the agents it actually booted with,
  // so a subagent added later shows up here with no UI change. `disabled` from
  // the live settings wins over the snapshot the roster was fetched with.
  const off = new Set(disabledSubagents)
  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      {roster.map((sa) => (
        <ToggleRow
          key={sa.name}
          title={sa.name}
          hint={sa.togglable
            ? (sa.description || '').slice(0, 160)
            : 'Always on — the master falls back to this one when it delegates.'}
          on={!off.has(sa.name)}
          disabled={saving || !sa.togglable}
          onToggle={() => setSubagentEnabled(sa.name, off.has(sa.name)).catch(() => {})}
        />
      ))}
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>
        A switched-off subagent is hidden from the orchestrator, chat and voice
        agents, and event triage stops routing work to it. Background tasks
        already running finish normally.
      </div>
    </div>
  )
}
