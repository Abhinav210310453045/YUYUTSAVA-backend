import React, { useState } from 'react'
import { providerModels, testModel } from '../../api/models'
import { Availability, Button, Muted, Panel, Problem, TextInput } from './ui'

// Settings → Models' rows that do something of their own: check a provider,
// name a model, send a (billable) test message.

export const subhead = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  fontSize: 12,
  color: 'var(--text-muted)',
  textTransform: 'uppercase',
  letterSpacing: 0.6,
  margin: '16px 0 8px',
}

export const select = {
  padding: '6px 8px',
  borderRadius: 'var(--radius-btn)',
  border: '1px solid var(--border-card)',
  background: 'var(--bg-elevated)',
  color: 'var(--text-primary)',
  fontFamily: 'var(--font-mono)',
  fontSize: 12,
}

export function ProviderRow({ provider, onRemove }) {
  const [listing, setListing] = useState(null)
  const [busy, setBusy] = useState(false)
  const settings = Object.entries(provider.settings)
    .map(([, v]) => v)
    .join(' · ')
  async function check() {
    setBusy(true)
    setListing(
      await providerModels(provider.name).catch((e) => ({ ok: false, problem: e.message })),
    )
    setBusy(false)
  }
  return (
    <Panel>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ color: 'var(--text-primary)' }}>{provider.name}</span>
        <Muted style={{ flex: 1 }}>
          {provider.label}
          {settings && ` · ${settings}`} · {provider.login}
        </Muted>
        <Button small disabled={busy} onClick={check} title="Log in and list its models (free)">
          {busy ? 'checking…' : 'check'}
        </Button>
        <Button small danger onClick={onRemove}>
          remove
        </Button>
      </div>
      {listing &&
        (listing.ok ? (
          <div style={{ marginTop: 8, maxHeight: 180, overflowY: 'auto' }}>
            {listing.models.map((m) => (
              <div key={m.id} style={{ fontSize: 12, display: 'flex', gap: 6 }}>
                <Availability value={m.availability} />
                <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}>
                  {m.id}
                </span>
                {m.note && <Muted style={{ display: 'inline' }}>— {m.note}</Muted>}
              </div>
            ))}
          </div>
        ) : (
          <div style={{ marginTop: 6 }}>
            <Problem>{listing.problem}</Problem>
            {listing.fix && <Muted>{listing.fix}</Muted>}
          </div>
        ))}
    </Panel>
  )
}

export function NewAlias({ providers, onAdd }) {
  const [name, setName] = useState('')
  const [provider, setProvider] = useState(providers[0]?.name || '')
  const [id, setId] = useState('')
  const [location, setLocation] = useState('')
  const kind = providers.find((p) => p.name === provider)?.type
  return (
    <Panel style={{ display: 'flex', gap: 6, alignItems: 'center', padding: '8px 12px' }}>
      <TextInput value={name} onChange={setName} placeholder="name" style={{ width: 120 }} />
      <select value={provider} onChange={(e) => setProvider(e.target.value)} style={select}>
        {providers.map((p) => (
          <option key={p.name}>{p.name}</option>
        ))}
      </select>
      <TextInput value={id} onChange={setId} placeholder="model id" />
      {kind === 'vertex' && (
        <TextInput
          value={location}
          onChange={setLocation}
          placeholder="location"
          style={{ width: 100 }}
        />
      )}
      <Button
        small
        disabled={!name || !provider || !id}
        onClick={() => {
          onAdd({ name, provider, id, ...(location ? { location } : {}) })
          setName('')
          setId('')
          setLocation('')
        }}
      >
        add
      </Button>
    </Panel>
  )
}

export function TestMessage({ choices }) {
  const [ref, setRef] = useState(choices[0] || '')
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  async function send() {
    setBusy(true)
    setResult(await testModel(ref).catch((e) => ({ ok: false, problem: e.message })))
    setBusy(false)
  }
  return (
    <>
      <div style={subhead}>Test a model</div>
      <Panel style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '8px 12px' }}>
        <select value={ref} onChange={(e) => setRef(e.target.value)} style={select}>
          {choices.map((c) => (
            <option key={c}>{c}</option>
          ))}
        </select>
        <Button
          small
          disabled={busy || !ref}
          onClick={send}
          title="Sends one short message — billed by the provider"
        >
          {busy ? 'sending…' : 'send a test message (billable)'}
        </Button>
        {result &&
          (result.ok ? (
            <Muted>
              ✓ “{result.reply}” in {result.seconds}s
            </Muted>
          ) : (
            <Problem>{result.problem}</Problem>
          ))}
      </Panel>
    </>
  )
}
