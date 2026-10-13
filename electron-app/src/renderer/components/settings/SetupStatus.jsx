import React, { useState, useEffect, useCallback } from 'react'
import {
  getSetupState,
  moveSettingsTo,
  runSetupCheck,
  startSetupService,
  startDockerEngine,
} from '../../api/setup'

// Does this part of the setup actually work, and can it be made to?
//
// The Settings form above renders every setting from the daemon's catalog, but
// a form cannot say whether what is in it works: a Postgres DSN looks the same
// whether or not the container is running. That gap is how a first run used to
// end — configured, saved, and a chat that died on "Postgres at
// 127.0.0.1:5435/yuyutsava is unavailable".
//
// Nothing here runs on its own. Checks and starts happen when asked for, the
// exact command is shown before it runs, and a failure is a problem and a fix,
// never a stack trace. It is the same catalog, the same probes and the same
// compose commands `yuyutsava setup` uses in a terminal.

const MONO = 'var(--font-mono)'

function Result({ check }) {
  if (!check) return null
  const ok = check.ok
  return (
    <div style={{ fontSize: 11, marginTop: 6, lineHeight: 1.5 }}>
      <div style={{ color: ok ? 'var(--neon-green)' : 'var(--text-primary)' }}>
        {ok ? '✓' : '✗'} {check.name}
        {ok && check.note ? ` — ${check.note}` : ''}
        {!ok && check.problem ? `: ${check.problem}` : ''}
      </div>
      {!ok && check.fix && (
        <div style={{ color: 'var(--text-muted)', marginTop: 2 }}>{check.fix}</div>
      )}
    </div>
  )
}

function Button({ onClick, busy, children, title }) {
  return (
    <button
      onClick={onClick}
      disabled={busy}
      title={title}
      style={{
        padding: '5px 11px',
        fontSize: 11,
        fontFamily: MONO,
        borderRadius: 'var(--radius-card)',
        border: '1px solid var(--border-card)',
        background: 'var(--bg-elevated)',
        color: busy ? 'var(--text-muted)' : 'var(--text-primary)',
        cursor: busy ? 'default' : 'pointer',
      }}
    >
      {busy ? '…' : children}
    </button>
  )
}

function Service({ service, onStarted }) {
  const [check, setCheck] = useState(null)
  const [busy, setBusy] = useState('')
  // The command is shown before anything runs it, so "Start" is a decision and
  // not a surprise — the same bargain the terminal wizard makes.
  const [confirming, setConfirming] = useState(false)

  const probe = async () => {
    setBusy('check')
    try {
      setCheck(await runSetupCheck(service.name))
    } catch (e) {
      setCheck({ name: service.name, ok: false, problem: String(e.message || e), fix: '' })
    } finally {
      setBusy('')
    }
  }

  const start = async () => {
    setConfirming(false)
    setBusy('start')
    try {
      const result = await startSetupService(service.name)
      setCheck(result)
      if (result.ok) onStarted?.()
    } catch (e) {
      setCheck({ name: service.name, ok: false, problem: String(e.message || e), fix: '' })
    } finally {
      setBusy('')
    }
  }

  return (
    <div
      style={{
        padding: '10px 0',
        borderTop: '1px solid var(--border-subtle)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span style={{ fontSize: 12, fontFamily: MONO, color: 'var(--text-primary)' }}>
          {service.name}
        </span>
        <span style={{ flex: 1 }} />
        <Button onClick={probe} busy={busy === 'check'}>
          Check
        </Button>
        <Button onClick={() => setConfirming(true)} busy={busy === 'start'}>
          Start
        </Button>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3, lineHeight: 1.5 }}>
        {service.purpose}
      </div>
      {service.needed_by?.length > 0 && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
          Needed by {service.needed_by.join(', ')}
        </div>
      )}
      {confirming && (
        <div
          style={{
            marginTop: 8,
            padding: '8px 11px',
            border: '1px solid var(--border-card)',
            borderRadius: 'var(--radius-card)',
            background: 'var(--bg-elevated)',
          }}
        >
          <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>This will run:</div>
          <div style={{ fontSize: 11, fontFamily: MONO, margin: '4px 0 8px' }}>
            {service.command}
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <Button onClick={start}>Run it</Button>
            <Button onClick={() => setConfirming(false)}>Cancel</Button>
          </div>
        </div>
      )}
      <Result check={check} />
    </div>
  )
}

function WhereSettingsLive({ source, onMoved }) {
  const [busy, setBusy] = useState(false)
  if (!source) return null
  const other = source.active.kind === 'settings' ? 'env' : 'settings'
  const otherPath = source.choices.find((one) => one.kind === other)?.path

  const move = async () => {
    setBusy(true)
    try {
      await moveSettingsTo(other)
      onMoved?.()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ paddingBottom: 10 }}>
      <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>Settings are kept in</div>
      <div style={{ fontSize: 11, fontFamily: MONO, margin: '3px 0 8px', wordBreak: 'break-all' }}>
        {source.active.path}
      </div>
      {source.overlapping?.length > 0 && (
        <div style={{ fontSize: 11, color: 'var(--text-primary)', marginBottom: 8 }}>
          ! {source.overlapping.join(', ')} are set in both files. The one above wins; the others
          are ignored.
        </div>
      )}
      {otherPath && (
        <Button onClick={move} busy={busy} title={otherPath}>
          Move them to {other === 'settings' ? 'settings.toml' : '.env'}
        </Button>
      )}
    </div>
  )
}

export default function SetupStatus() {
  const [state, setState] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setState(await getSetupState())
      setError('')
    } catch (e) {
      setError(String(e.message || e))
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const launch = async () => {
    setBusy(true)
    try {
      await startDockerEngine()
      await load()
    } finally {
      setBusy(false)
    }
  }

  if (error) {
    return (
      <div style={{ fontSize: 11, color: 'var(--text-muted)', padding: '8px 0' }}>
        The daemon could not be asked about setup ({error}). Start it above, or run{' '}
        <span style={{ fontFamily: MONO }}>yuyutsava config check</span> in a terminal.
      </div>
    )
  }
  if (!state) return <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Loading…</div>

  return (
    <div style={{ padding: '4px 2px' }}>
      <WhereSettingsLive source={state.source} onMoved={load} />

      <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 12, fontFamily: MONO }}>Docker</span>
          <span style={{ flex: 1 }} />
          {!state.engine.ok && state.engine_launch_command && (
            <Button onClick={launch} busy={busy} title={state.engine_launch_command}>
              Start Docker Desktop
            </Button>
          )}
          <Button onClick={load}>Re-check</Button>
        </div>
        <Result check={state.engine} />
      </div>

      {state.services.map((service) => (
        <Service key={service.name} service={service} onStarted={load} />
      ))}
    </div>
  )
}
