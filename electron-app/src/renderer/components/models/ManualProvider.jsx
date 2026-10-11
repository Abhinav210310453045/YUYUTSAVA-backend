import React, { useState } from 'react'
import { checkProvider } from '../../api/models'
import { Button, Labelled, Muted, Panel, Problem, TextInput, found } from './ui'
import { actions, card, heading } from './wizardStyles'

// A provider by hand: its type, the settings that type needs, how it logs in —
// asked from GET /models/catalog, the same forms the CLI wizard asks from. It is
// named for the person (freeName), never by them: the name only tells two
// connections of one kind apart.

export const capitalised = (label) => label.charAt(0).toUpperCase() + label.slice(1)

export default function Manual({ catalog, freeName, onAdd, onBack }) {
  const [type, setType] = useState('')
  const form = catalog.find((f) => f.type === type)
  const [name, setName] = useState('') // set by choose(): freeName(type)
  const [settings, setSettings] = useState({})
  const [method, setMethod] = useState('')
  const [fields, setFields] = useState({})
  const [check, setCheck] = useState(null)
  const [busy, setBusy] = useState(false)

  function choose(kind) {
    const f = catalog.find((x) => x.type === kind)
    setType(kind)
    setName(freeName(kind))
    setSettings(Object.fromEntries(f.settings.map((s) => [s.name, s.default])))
    setMethod(f.logins[0].method)
    setFields({})
    setCheck(null)
  }

  const login = form?.logins.find((l) => l.method === method)
  const connection = () => ({
    type,
    ...settings,
    auth: {
      method,
      ...Object.fromEntries(
        (login?.fields || [])
          .filter((f) => !f.secret)
          .map((f) => [f.name, fields[f.name] ?? f.default]),
      ),
    },
  })
  const secrets = () =>
    Object.fromEntries(
      (login?.fields || []).filter((f) => f.secret).map((f) => [f.name, fields[f.name] || '']),
    )

  async function runCheck() {
    setBusy(true)
    const result = await checkProvider(name, connection(), secrets()).catch((e) => ({
      ok: false,
      problem: e.message,
    }))
    setCheck(result)
    setBusy(false)
  }

  if (!form) {
    return (
      <>
        <div style={heading}>Which provider do you want to connect?</div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          {catalog.map((f) => (
            <div key={f.type} onClick={() => choose(f.type)} className="tap-pop" style={card}>
              <div style={{ color: 'var(--text-primary)', fontSize: 13 }}>
                {capitalised(f.label)}
              </div>
              <Muted style={{ marginTop: 3 }}>{f.introduction}</Muted>
            </div>
          ))}
        </div>
        {onBack && (
          <div style={actions}>
            <Button onClick={onBack}>back</Button>
          </div>
        )}
      </>
    )
  }
  return (
    <>
      <div style={heading}>
        {capitalised(form.label)} <Muted style={{ display: 'inline' }}>— {form.introduction}</Muted>
      </div>
      {form.settings.map((s) => (
        <Labelled key={s.name} label={s.question}>
          <TextInput
            value={settings[s.name] ?? ''}
            onChange={(v) => setSettings((c) => ({ ...c, [s.name]: v }))}
          />
        </Labelled>
      ))}
      {form.logins.length > 1 && (
        <Labelled label={`How should YUYUTSAVA log in to ${form.label}?`}>
          {form.logins.map((l) => (
            <label
              key={l.method}
              style={{
                display: 'flex',
                gap: 8,
                fontSize: 12,
                color: 'var(--text-secondary)',
                marginBottom: 4,
              }}
            >
              <input
                type="radio"
                checked={method === l.method}
                onChange={() => setMethod(l.method)}
              />
              {l.label}
            </label>
          ))}
        </Labelled>
      )}
      {login?.fields.map((f) => (
        <Labelled
          key={f.name}
          label={f.question}
          hint={f.secret ? 'stays on this computer, readable only by you' : ''}
        >
          <TextInput
            secret={f.secret}
            value={fields[f.name] ?? (f.secret ? '' : f.default)}
            onChange={(v) => setFields((c) => ({ ...c, [f.name]: v }))}
          />
        </Labelled>
      ))}
      {check && (
        <Panel>
          {check.ok ? (
            <span style={{ color: 'var(--neon-green)', fontSize: 12 }}>
              ✓ {capitalised(form.label)} works: {found(check.models)}
            </span>
          ) : (
            <>
              <Problem>{check.problem}</Problem>
              {check.fix && <Muted>{check.fix}</Muted>}
            </>
          )}
        </Panel>
      )}
      <div style={actions}>
        <Button onClick={() => setType('')}>choose another provider</Button>
        <Button disabled={busy || !name} onClick={runCheck}>
          {busy ? 'checking…' : 'check the login (free)'}
        </Button>
        <Button
          primary
          disabled={!check}
          title={check?.ok ? '' : 'keep it anyway, and fix the login later'}
          onClick={() =>
            onAdd({ name, label: capitalised(form.label), connection: check.connection, check })
          }
        >
          {check && !check.ok ? 'add anyway' : 'add'}
        </Button>
      </div>
    </>
  )
}
