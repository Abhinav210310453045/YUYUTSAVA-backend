import React, { useState } from 'react'
import { aliasFor } from './aliasFor'
import {
  Availability,
  Button,
  Muted,
  Panel,
  Problem,
  TextInput,
  contextWindow,
  found as foundModels,
} from './ui'
import { actions, heading, optionRow } from './wizardStyles'

// The setup wizard's steps that choose: what this machine has, the providers
// checked so far, and the default model.

const OFFERED = 12

export function Found({ found, picked, setPicked, busy, onCheck, onManual }) {
  if (found === null) return <Muted>Looking at what this computer already has…</Muted>
  const toggle = (i) =>
    setPicked((cur) => (cur.includes(i) ? cur.filter((x) => x !== i) : [...cur, i]))
  return (
    <>
      <div style={heading}>Found on this computer — which should YUYUTSAVA use?</div>
      {found.map((f, i) => (
        <Panel key={`${f.name}-${i}`} style={{ cursor: 'pointer' }}>
          <label style={{ display: 'flex', gap: 10, alignItems: 'center', cursor: 'pointer' }}>
            <input type="checkbox" checked={picked.includes(i)} onChange={() => toggle(i)} />
            <span style={{ color: 'var(--text-primary)' }}>{f.label}</span>
            <Muted>{f.origin}</Muted>
          </label>
        </Panel>
      ))}
      <div style={actions}>
        <Button onClick={onManual}>connect a different provider</Button>
        <Button primary disabled={busy || !picked.length} onClick={onCheck}>
          {busy ? 'checking…' : 'check and continue'}
        </Button>
      </div>
    </>
  )
}

export function Review({ providers, setProviders, onManual, onNext, busy, mode }) {
  const drop = (name) => setProviders((cur) => cur.filter((p) => p.name !== name))
  return (
    <>
      <div style={heading}>Your providers</div>
      {providers.map((p) => (
        <Panel key={p.name}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ color: p.check?.ok ? 'var(--neon-green)' : 'var(--neon-amber)' }}>
              {p.check?.ok ? '✓' : '!'}
            </span>
            <span style={{ color: 'var(--text-primary)', flex: 1 }}>
              {p.label || p.connection.type}
              {p.name !== p.connection.type && (
                <Muted style={{ display: 'inline' }}> (saved as {p.name})</Muted>
              )}
            </span>
            <Button small onClick={() => drop(p.name)}>
              leave out
            </Button>
          </div>
          {p.check?.ok ? (
            <Muted style={{ marginTop: 4 }}>works: {foundModels(p.check.models)}</Muted>
          ) : (
            <div style={{ marginTop: 4 }}>
              <Problem>{p.check?.problem || 'not checked'}</Problem>
              {p.check?.fix && <Muted>{p.check.fix}</Muted>}
              <Muted>You can keep it and fix the login later.</Muted>
            </div>
          )}
        </Panel>
      ))}
      <div style={actions}>
        <Button onClick={onManual}>connect another provider</Button>
        <Button primary disabled={busy || !providers.length} onClick={onNext}>
          {mode === 'add' ? (busy ? 'saving…' : 'save') : 'continue'}
        </Button>
      </div>
    </>
  )
}

export function ChooseDefault({ providers, busy, onBack, onSave }) {
  const options = []
  for (const p of providers) {
    const listed = p.check?.models || []
    if (p.model) {
      const hasSettings = p.model_settings && Object.keys(p.model_settings).length
      options.push({
        ref: hasSettings ? aliasFor(p.model) : `${p.name}/${p.model}`,
        shown: p.model,
        label: p.label,
        hint: 'what YUYUTSAVA uses today',
        model: listed.find((m) => m.id === p.model),
      })
    }
    for (const m of listed.filter((x) => x.availability === 'available').slice(0, OFFERED)) {
      // The model in use today is offered once, as itself, above.
      if (m.id !== p.model)
        options.push({ ref: `${p.name}/${m.id}`, shown: m.id, label: p.label, model: m })
    }
  }
  const [chosen, setChosen] = useState(options[0]?.ref || '')
  const [typed, setTyped] = useState('')
  // A name typed on its own is taken to be on the first provider.
  const asTyped = typed.includes('/') || !providers.length ? typed : `${providers[0].name}/${typed}`
  const value = chosen === '__typed' ? asTyped.trim() && asTyped : chosen
  return (
    <>
      <div style={heading}>Which model should YUYUTSAVA use?</div>
      <Muted style={{ marginBottom: 10 }}>
        You can change it any time — in a chat with the model chip, or in Settings → Models.
      </Muted>
      <div style={{ maxHeight: 300, overflowY: 'auto', marginBottom: 8 }}>
        {options.map((o) => (
          <label key={o.ref} style={optionRow}>
            <input type="radio" checked={chosen === o.ref} onChange={() => setChosen(o.ref)} />
            {o.model && <Availability value={o.model.availability} />}
            <span
              style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-primary)' }}
            >
              {o.shown}
            </span>
            {o.label && <Muted style={{ display: 'inline' }}>· {o.label}</Muted>}
            {o.hint && <Muted style={{ display: 'inline' }}>— {o.hint}</Muted>}
            {o.model?.context_window && (
              <Muted style={{ display: 'inline' }}>— {contextWindow(o.model.context_window)}</Muted>
            )}
          </label>
        ))}
        <label style={optionRow}>
          <input
            type="radio"
            checked={chosen === '__typed'}
            onChange={() => setChosen('__typed')}
          />
          <span style={{ fontSize: 12, color: 'var(--text-secondary)', flexShrink: 0 }}>
            another model:
          </span>
          <TextInput
            value={typed}
            onChange={(v) => {
              setTyped(v)
              setChosen('__typed')
            }}
            placeholder="its name, e.g. gemini-3.5-flash"
          />
        </label>
      </div>
      <div style={actions}>
        <Button onClick={onBack}>back</Button>
        <Button primary disabled={busy || !value} onClick={() => onSave(value)}>
          {busy ? 'saving…' : 'save'}
        </Button>
      </div>
    </>
  )
}
