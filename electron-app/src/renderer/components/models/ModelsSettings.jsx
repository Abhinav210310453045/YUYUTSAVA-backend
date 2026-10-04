import React, { useState } from 'react'
import {
  addAlias,
  modelChoices,
  removeAlias,
  removeProvider,
  setDefault,
  setTasks,
} from '../../api/models'
import { useModels } from '../../hooks/useModels'
import SetupWizard from './SetupWizard'
import { NewAlias, ProviderRow, TestMessage, select, subhead } from './ModelsSettingsParts'
import { Button, Muted, Panel, Problem } from './ui'

// Settings → Models: everything models.toml says, changeable in place. Every
// change sends the revision this screen read; if the file changed meanwhile (the
// CLI, another window, a hand edit) the daemon refuses it and the screen reloads
// rather than overwrite what someone else wrote.
export default function ModelsSettings() {
  const { status, error, refresh } = useModels()
  const [wizard, setWizard] = useState(null) // 'setup' | 'add'
  const [notice, setNotice] = useState('')

  async function change(fn, done) {
    setNotice('')
    try {
      await fn()
      if (done) setNotice(done)
    } catch (e) {
      setNotice(
        e.status === 409 ? 'models.toml changed elsewhere — reloaded; try again.' : e.message,
      )
      if (e.status === 409) refresh()
    }
  }

  if (error && !status) return <Problem>Could not read the models: {error.message}</Problem>
  if (!status) return <Muted>Loading…</Muted>

  const wizardView = wizard && (
    <SetupWizard
      status={status}
      mode={wizard}
      onDone={() => {
        setWizard(null)
        refresh()
      }}
      onClose={() => setWizard(null)}
    />
  )

  if (!status.configured) {
    return (
      <>
        {wizardView}
        <Panel>
          {status.problem ? (
            <>
              <Problem>{status.problem}</Problem>
              <Muted style={{ marginTop: 6 }}>Fix it in {status.path}, or set up again.</Muted>
            </>
          ) : (
            <Muted>
              No models.toml yet — models come from the environment (.env). Set one up to pick
              models here, per task and per conversation.
            </Muted>
          )}
          <div style={{ marginTop: 10 }}>
            <Button primary onClick={() => setWizard('setup')}>
              set up models
            </Button>
          </div>
        </Panel>
      </>
    )
  }

  const choices = modelChoices(status)
  const rev = status.revision
  return (
    <>
      {wizardView}
      {notice && <Muted style={{ marginBottom: 8, color: 'var(--text-info)' }}>{notice}</Muted>}
      {status.secrets_missing.map((s) => (
        <Problem key={s.reference}>
          {s.reference}: {s.problem} — {s.fix}
        </Problem>
      ))}

      <Row label="Default model">
        <Choice
          value={status.default}
          choices={choices}
          onChange={(ref) => change(() => setDefault(rev, ref), `default is now ${ref}`)}
        />
      </Row>

      <div style={subhead}>Tasks</div>
      {status.tasks.map((t) => (
        <Row key={t.name} label={t.label} hint={t.name}>
          <Choice
            value={t.follows_default ? '' : t.reference}
            choices={choices}
            followLabel={`follows the default (${status.default})`}
            onChange={(ref) =>
              change(
                () => setTasks(rev, [t.name], ref || null),
                `${t.name} → ${ref || 'the default'}`,
              )
            }
          />
        </Row>
      ))}

      <div style={subhead}>
        Providers
        <Button small onClick={() => setWizard('add')}>
          add provider
        </Button>
      </div>
      {status.providers.map((p) => (
        <ProviderRow
          key={p.name}
          provider={p}
          onRemove={() => change(() => removeProvider(rev, p.name), `${p.name} removed`)}
        />
      ))}

      <div style={subhead}>Model names</div>
      {status.models.map((m) => (
        <Panel
          key={m.name}
          style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px' }}
        >
          <span
            style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-primary)' }}
          >
            {m.name}
          </span>
          <Muted style={{ flex: 1 }}>
            {m.provider} · {m.id}
            {Object.entries(m.settings).map(
              ([k, v]) => ` · ${k} ${typeof v === 'object' ? JSON.stringify(v) : v}`,
            )}
          </Muted>
          <Button small onClick={() => change(() => removeAlias(rev, m.name), `${m.name} removed`)}>
            remove
          </Button>
        </Panel>
      ))}
      <NewAlias
        providers={status.providers}
        onAdd={(alias) => change(() => addAlias(rev, alias), `${alias.name} added`)}
      />

      <TestMessage choices={choices} />
      <Muted style={{ marginTop: 10 }}>{status.path}</Muted>
    </>
  )
}

function Row({ label, hint, children }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
      <div style={{ width: 230, fontSize: 12, color: 'var(--text-secondary)' }}>
        {label}
        {hint && (
          <span style={{ color: 'var(--text-dim)', marginLeft: 6, fontFamily: 'var(--font-mono)' }}>
            {hint}
          </span>
        )}
      </div>
      <div style={{ flex: 1 }}>{children}</div>
    </div>
  )
}

function Choice({ value, choices, onChange, followLabel }) {
  const options = followLabel ? ['', ...choices] : choices
  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value)}
      style={{ ...select, width: '100%' }}
    >
      {options.map((c) => (
        <option key={c || '__follow'} value={c}>
          {c || followLabel}
        </option>
      ))}
    </select>
  )
}
