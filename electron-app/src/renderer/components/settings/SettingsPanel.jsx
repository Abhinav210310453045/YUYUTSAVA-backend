import React, { useState, useEffect } from 'react'
import SettingsSection from './SettingsSection'
import SettingsField from './SettingsField'

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

const LLM_PROVIDERS = [
  { value: 'groq', label: 'Groq' },
  { value: 'openrouter', label: 'OpenRouter' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'ollama', label: 'Ollama (local)' },
]

export default function SettingsPanel() {
  const [settings, setSettings] = useState({})
  const [loading, setLoading] = useState(true)
  const [saved, setSaved] = useState(false)
  const [daemonStatus, setDaemonStatus] = useState(null)
  const [daemonBusy, setDaemonBusy] = useState(false)

  useEffect(() => {
    Promise.all([
      window.electronAPI?.getSettings() || Promise.resolve({}),
      window.electronAPI?.getDaemonStatus() || Promise.resolve(null),
    ]).then(([s, status]) => {
      setSettings(s || {})
      setDaemonStatus(status)
      setLoading(false)
    })
  }, [])

  async function refreshStatus() {
    const status = await window.electronAPI?.getDaemonStatus()
    setDaemonStatus(status)
  }

  function onChange(key, val) {
    setSettings(prev => ({ ...prev, [key]: val }))
  }

  async function save() {
    await window.electronAPI?.saveSettings(settings)
    setSaved(true)
    setTimeout(() => setSaved(false), 4000)
    await refreshStatus()
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

  const provider = settings['LLM_PROVIDER'] || ''

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
        <h2 style={{ fontSize: 13, fontWeight: 600, fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.1em' }}>
          Settings
        </h2>
        <button
          onClick={save}
          style={{
            padding: '6px 18px',
            borderRadius: 'var(--radius-btn)',
            fontSize: 11,
            fontFamily: 'var(--font-mono)',
            fontWeight: 600,
            letterSpacing: '0.05em',
            textTransform: 'uppercase',
            border: '1px solid',
            cursor: 'pointer',
            color: saved ? 'var(--neon-green)' : 'var(--text-primary)',
            borderColor: saved ? 'rgba(0,255,136,0.4)' : 'var(--border-card)',
            background: saved ? 'rgba(0,255,136,0.08)' : 'var(--bg-elevated)',
            transition: 'all 0.2s',
          }}
        >
          {saved ? '✓ Saved' : 'Save'}
        </button>
      </div>

      {saved && (
        <div style={{
          background: 'rgba(0,255,136,0.06)',
          border: '1px solid rgba(0,255,136,0.2)',
          borderRadius: 'var(--radius-card)',
          padding: '9px 14px',
          fontSize: 11,
          color: 'var(--neon-green)',
          fontFamily: 'var(--font-mono)',
        }}>
          ✓ Settings saved — restart the daemon above to apply changes.
        </div>
      )}

      {/* Daemon status + controls */}
      <div style={{
        background: 'var(--bg-elevated)',
        border: `1px solid ${daemonStatus?.running ? 'rgba(0,255,136,0.15)' : 'rgba(255,51,102,0.15)'}`,
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
          <span style={{ color: daemonStatus?.running ? 'var(--neon-green)' : 'var(--neon-red)', fontWeight: 600 }}>
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
            borderColor="rgba(0,255,136,0.3)"
            bg="rgba(0,255,136,0.06)"
            disabled={daemonBusy}
            onClick={startDaemon}
          />
        )}
      </div>

      <SettingsSection title="Daemon" defaultOpen={true}>
        <SettingsField label="Port" envKey="YUYUTSAVA_DAEMON_PORT" type="number" value={settings['YUYUTSAVA_DAEMON_PORT']} onChange={onChange} placeholder="7654" />
        <SettingsField label="Proposal expiry (seconds)" envKey="YUYUTSAVA_PROPOSAL_EXPIRY_SEC" type="number" value={settings['YUYUTSAVA_PROPOSAL_EXPIRY_SEC']} onChange={onChange} placeholder="300" />
        <SettingsField label="Orchestrator token budget" envKey="YUYUTSAVA_ORCHESTRATOR_TOKEN_BUDGET" type="number" value={settings['YUYUTSAVA_ORCHESTRATOR_TOKEN_BUDGET']} onChange={onChange} placeholder="8000" />
        <SettingsField label="Subagent token budget" envKey="YUYUTSAVA_SUBAGENT_TOKEN_BUDGET" type="number" value={settings['YUYUTSAVA_SUBAGENT_TOKEN_BUDGET']} onChange={onChange} placeholder="30000" />
        <SettingsField label="Heartbeat interval (seconds)" envKey="YUYUTSAVA_HEARTBEAT_SEC" type="number" value={settings['YUYUTSAVA_HEARTBEAT_SEC']} onChange={onChange} placeholder="30" />
        <SettingsField label="Home directory" envKey="YUYUTSAVA_HOME" value={settings['YUYUTSAVA_HOME']} onChange={onChange} placeholder="~/.yuyutsava" />
        <SettingsField label="Output directory" envKey="YUYUTSAVA_OUTPUT_DIR" value={settings['YUYUTSAVA_OUTPUT_DIR']} onChange={onChange} />
        <SettingsField label="Managed by app" envKey="YUYUTSAVA_DAEMON_MANAGED" type="toggle" value={settings['YUYUTSAVA_DAEMON_MANAGED'] ?? 'true'} onChange={onChange} />
      </SettingsSection>

      <SettingsSection title="LLM Provider" defaultOpen={true}>
        <SettingsField label="Provider" envKey="LLM_PROVIDER" type="select" value={settings['LLM_PROVIDER']} onChange={onChange} options={LLM_PROVIDERS} />

        {(!provider || provider === 'groq') && <>
          <SettingsField label="Groq API Key" envKey="GROQ_API_KEY" type="password" value={settings['GROQ_API_KEY']} onChange={onChange} placeholder="gsk_..." />
          <SettingsField label="Groq Model" envKey="GROQ_MODEL" value={settings['GROQ_MODEL']} onChange={onChange} placeholder="llama-3.1-8b-instant" />
        </>}

        {provider === 'openrouter' && <>
          <SettingsField label="OpenRouter API Key" envKey="OPENROUTER_API_KEY" type="password" value={settings['OPENROUTER_API_KEY']} onChange={onChange} placeholder="sk-or-..." />
          <SettingsField label="OpenRouter Model" envKey="OPENROUTER_MODEL" value={settings['OPENROUTER_MODEL']} onChange={onChange} placeholder="anthropic/claude-3-haiku" />
        </>}

        {provider === 'anthropic' && <>
          <SettingsField label="Anthropic API Key" envKey="ANTHROPIC_API_KEY" type="password" value={settings['ANTHROPIC_API_KEY']} onChange={onChange} placeholder="sk-ant-..." />
          <SettingsField label="Anthropic Model" envKey="ANTHROPIC_MODEL" value={settings['ANTHROPIC_MODEL']} onChange={onChange} placeholder="claude-haiku-4-5-20251001" />
        </>}

        {provider === 'ollama' && <>
          <SettingsField label="Ollama Host" envKey="OLLAMA_HOST" value={settings['OLLAMA_HOST']} onChange={onChange} placeholder="http://localhost:11434" />
          <SettingsField label="Ollama Model" envKey="OLLAMA_MODEL" value={settings['OLLAMA_MODEL']} onChange={onChange} placeholder="llama3.2:3b" />
        </>}
      </SettingsSection>

      <SettingsSection title="Search" defaultOpen={false}>
        <SettingsField label="Tavily API Key" envKey="TAVILY_API_KEY" type="password" value={settings['TAVILY_API_KEY']} onChange={onChange} placeholder="tvly-..." />
        <SettingsField label="Exa API Key" envKey="EXA_API_KEY" type="password" value={settings['EXA_API_KEY']} onChange={onChange} />
      </SettingsSection>

      <SettingsSection title="Docker Sandbox" defaultOpen={false}>
        <SettingsField label="Image" envKey="YUYUTSAVA_DOCKER_IMAGE" value={settings['YUYUTSAVA_DOCKER_IMAGE']} onChange={onChange} placeholder="python:3.12-slim" />
        <SettingsField label="Network" envKey="YUYUTSAVA_DOCKER_NETWORK" type="select" value={settings['YUYUTSAVA_DOCKER_NETWORK']} onChange={onChange} options={[{ value: 'bridge', label: 'bridge' }, { value: 'none', label: 'none' }]} />
        <SettingsField label="Memory limit" envKey="YUYUTSAVA_DOCKER_MEMORY" value={settings['YUYUTSAVA_DOCKER_MEMORY']} onChange={onChange} placeholder="512m" />
        <SettingsField label="CPU limit" envKey="YUYUTSAVA_DOCKER_CPUS" value={settings['YUYUTSAVA_DOCKER_CPUS']} onChange={onChange} placeholder="1.0" />
        <SettingsField label="PIDs limit" envKey="YUYUTSAVA_DOCKER_PIDS_LIMIT" type="number" value={settings['YUYUTSAVA_DOCKER_PIDS_LIMIT']} onChange={onChange} placeholder="64" />
        <SettingsField label="Export directory" envKey="YUYUTSAVA_DOCKER_EXPORT_DIR" value={settings['YUYUTSAVA_DOCKER_EXPORT_DIR']} onChange={onChange} />
      </SettingsSection>
    </div>
  )
}
