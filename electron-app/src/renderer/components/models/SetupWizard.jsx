import React, { useEffect, useMemo, useState } from 'react'
import { checkProvider, getCatalog, getDetected, saveSetup } from '../../api/models'
import { aliasFor } from './aliasFor'
import ManualProvider from './ManualProvider'
import SetupStatus from '../settings/SetupStatus'
import { Button, Muted, Problem } from './ui'
import { ChooseDefault, Found, Review } from './wizardSteps'
import { overlay, sheet } from './wizardStyles'

// Connecting a model, one step at a time — the app's twin of `yuyutsava models
// setup`, asking the same questions (GET /models/catalog) and writing through
// the same store:
//
//   found      what this machine already has, the current setup picked first
//   review     each provider's login checked (free), fixed, kept or dropped
//   manual     a provider by hand: its type, its settings, how it logs in
//   default    the model YUYUTSAVA uses unless told otherwise
//   starting   the daemon was waiting to be set up; it restarts on the file
//
// Nothing is written until the last step, in one request. Keys typed here go
// to secrets.toml when their login is checked; the file only names them.
// `mode="add"` skips what this machine has and the default, for Settings → Models.

export default function SetupWizard({ status, mode = 'setup', onDone, onClose }) {
  const [step, setStep] = useState(mode === 'add' ? 'manual' : 'found')
  const [catalog, setCatalog] = useState([])
  const [found, setFound] = useState(null)
  const [picked, setPicked] = useState([])
  const [providers, setProviders] = useState([]) // {name, connection, model, model_settings, check}
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState('')

  useEffect(() => {
    getCatalog()
      .then(setCatalog)
      .catch((e) => setProblem(e.message))
    if (mode !== 'add') {
      getDetected()
        .then((list) => {
          setFound(list)
          setPicked(list.length ? [0] : [])
          if (!list.length) setStep('manual')
        })
        .catch(() => setFound([]))
    }
  }, [mode])

  const taken = useMemo(
    () =>
      new Set([...(status?.providers || []).map((p) => p.name), ...providers.map((p) => p.name)]),
    [status, providers],
  )
  const freeName = (wanted) => {
    let name = wanted
    for (let n = 2; taken.has(name); n += 1) name = `${wanted}-${n}`
    return name
  }

  async function checkFound() {
    setBusy(true)
    const checked = []
    for (const index of picked) {
      const f = found[index]
      const name = freeName(f.name)
      const result = await checkProvider(name, f.connection).catch((e) => ({
        ok: false,
        problem: e.message,
      }))
      checked.push({
        name,
        label: f.label,
        connection: result.connection || f.connection,
        model: f.model,
        model_settings: f.model_settings,
        check: result,
      })
    }
    setProviders((cur) => [...cur, ...checked])
    setBusy(false)
    setStep('review')
  }

  async function save(defaultRef) {
    setBusy(true)
    setProblem('')
    const aliases = providers
      .filter((p) => p.model && p.model_settings && Object.keys(p.model_settings).length)
      .map((p) => ({ name: aliasFor(p.model), provider: p.name, id: p.model, ...p.model_settings }))
    try {
      await saveSetup({
        revision: status.revision,
        providers: providers.map((p) => ({ name: p.name, connection: p.connection })),
        aliases,
        default: defaultRef || null,
      })
      if (status.setup_mode) setStep('starting')
      else setStep('rest')
    } catch (e) {
      setProblem(
        e.status === 409
          ? 'models.toml changed while this was open — close and open setup again.'
          : e.message,
      )
    }
    setBusy(false)
  }

  return (
    <div style={overlay}>
      <div style={sheet}>
        <div style={{ display: 'flex', alignItems: 'baseline', marginBottom: 14 }}>
          <div
            style={{
              fontSize: 18,
              fontWeight: 'var(--fw-semibold)',
              color: 'var(--text-primary)',
              flex: 1,
            }}
          >
            {mode === 'add' ? 'Connect another provider' : 'Connect YUYUTSAVA to an AI model'}
          </div>
          {onClose && !status?.setup_mode && (
            <Button small onClick={onClose}>
              close
            </Button>
          )}
        </div>
        {status?.setup_mode && step !== 'starting' && (
          <Muted style={{ marginBottom: 14 }}>
            YUYUTSAVA needs a model before it can start.{' '}
            {status.problem ? '' : 'Pick what to use below.'}
          </Muted>
        )}
        {problem && <Problem>{problem}</Problem>}

        {step === 'found' && (
          <Found
            found={found}
            picked={picked}
            setPicked={setPicked}
            busy={busy}
            onCheck={checkFound}
            onManual={() => setStep('manual')}
          />
        )}
        {step === 'review' && (
          <Review
            providers={providers}
            setProviders={setProviders}
            onManual={() => setStep('manual')}
            onNext={() => (mode === 'add' ? save(null) : setStep('default'))}
            busy={busy}
            mode={mode}
          />
        )}
        {step === 'manual' && (
          <ManualProvider
            catalog={catalog}
            freeName={freeName}
            onAdd={(p) => {
              setProviders((cur) => [...cur, p])
              setStep('review')
            }}
            onBack={
              providers.length || mode !== 'add'
                ? () => setStep(providers.length ? 'review' : 'found')
                : null
            }
          />
        )}
        {step === 'default' && (
          <ChooseDefault
            providers={providers}
            busy={busy}
            onBack={() => setStep('review')}
            onSave={save}
          />
        )}
        {step === 'starting' && <Starting status={status} onDone={() => setStep('rest')} />}
        {step === 'rest' && <TheRest onDone={onDone} />}
      </div>
    </div>
  )
}

function Starting({ status, onDone }) {
  useEffect(() => {
    if (status && !status.setup_mode) onDone?.()
  }, [status, onDone])
  return <Muted>Saved. Starting YUYUTSAVA with your models…</Muted>
}

// A model is the first thing YUYUTSAVA needs, not the only one. Storage, web
// search and the diagram renderer each need a key or a running container, and a
// setup that stopped here left them unasked — which is how a first run finished
// successfully and the next chat died on a Postgres that was never started.
// Nothing here runs on its own; every check and every container is a button.
function TheRest({ onDone }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <Muted>
        Your models are set up. These are the rest of the things YUYUTSAVA can use — each one is
        optional, and you can come back to them in Settings.
      </Muted>
      <SetupStatus />
      <div>
        <button
          onClick={() => onDone?.()}
          style={{
            padding: '6px 14px',
            fontSize: 12,
            fontFamily: 'var(--font-mono)',
            borderRadius: 'var(--radius-card)',
            border: '1px solid var(--border-card)',
            background: 'var(--bg-elevated)',
            color: 'var(--text-primary)',
            cursor: 'pointer',
          }}
        >
          Done
        </button>
      </div>
    </div>
  )
}
