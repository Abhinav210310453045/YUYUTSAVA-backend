import React, { useState } from 'react'
import { aliasFor } from './aliasFor'
import { Availability, Button, Muted, Panel, Problem, TextInput } from './ui'
import { actions, heading, optionRow } from './wizardStyles'

// The setup wizard's steps that choose: what this machine has, the providers
// checked so far, and the default model.

const OFFERED = 12

export function Found({ found, picked, setPicked, busy, onCheck, onManual }) {
  if (found === null) return <Muted>Looking at what this machine already has…</Muted>
  const toggle = (i) =>
    setPicked((cur) => (cur.includes(i) ? cur.filter((x) => x !== i) : [...cur, i]))
  return (
    <>
      <div style={heading}>Found on this machine</div>
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
        <Button onClick={onManual}>set up a provider myself</Button>
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
      <div style={heading}>Providers</div>
      {providers.map((p) => (
        <Panel key={p.name}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ color: p.check?.ok ? 'var(--neon-green)' : 'var(--neon-amber)' }}>
              {p.check?.ok ? '✓' : '!'}
            </span>
            <span style={{ color: 'var(--text-primary)', flex: 1 }}>
              {p.name} <Muted style={{ display: 'inline' }}>· {p.connection.type}</Muted>
            </span>
            <Button small onClick={() => drop(p.name)}>
              leave out
            </Button>
          </div>
          {p.check?.ok ? (
            <Muted style={{ marginTop: 4 }}>
              logged in — {p.check.models.length} models,{' '}
              {p.check.models.filter((m) => m.availability === 'available').length} ready
            </Muted>
          ) : (
            <div style={{ marginTop: 4 }}>
              <Problem>{p.check?.problem || 'not checked'}</Problem>
              {p.check?.fix && <Muted>{p.check.fix}</Muted>}
              <Muted>It can be kept and fixed later.</Muted>
            </div>
          )}
        </Panel>
      ))}
      <div style={actions}>
        <Button onClick={onManual}>add another provider</Button>
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
    if (p.model) {
      const hasSettings = p.model_settings && Object.keys(p.model_settings).length
      options.push({
        ref: hasSettings ? aliasFor(p.model) : `${p.name}/${p.model}`,
        hint: 'what this machine uses now',
      })
    }
    for (const m of (p.check?.models || [])
      .filter((x) => x.availability === 'available')
      .slice(0, OFFERED)) {
      const ref = `${p.name}/${m.id}`
      if (!options.some((o) => o.ref === ref)) options.push({ ref, model: m })
    }
  }
  const [chosen, setChosen] = useState(options[0]?.ref || '')
  const [typed, setTyped] = useState('')
  const value = chosen === '__typed' ? typed : chosen
  return (
    <>
      <div style={heading}>Which model should YUYUTSAVA use by default?</div>
      <Muted style={{ marginBottom: 10 }}>
        Every task uses it unless given its own — change that in Settings → Models.
      </Muted>
      <div style={{ maxHeight: 300, overflowY: 'auto', marginBottom: 8 }}>
        {options.map((o) => (
          <label key={o.ref} style={optionRow}>
            <input type="radio" checked={chosen === o.ref} onChange={() => setChosen(o.ref)} />
            {o.model && <Availability value={o.model.availability} />}
            <span
              style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-primary)' }}
            >
              {o.ref}
            </span>
            {o.hint && <Muted style={{ display: 'inline' }}>{o.hint}</Muted>}
            {o.model?.context_window && (
              <Muted style={{ display: 'inline' }}>
                {Math.round(o.model.context_window / 1000)}k
              </Muted>
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
            type one
          </span>
          <TextInput
            value={typed}
            onChange={(v) => {
              setTyped(v)
              setChosen('__typed')
            }}
            placeholder="provider/model-id"
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
