import { getBase } from './client'

// The daemon's /models API: models.toml as the app reads and changes it. Every
// change sends the `revision` GET /models returned; a file changed elsewhere in
// the meantime comes back as a ModelsError with status 409, never overwritten.

export class ModelsError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

async function call(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } }
  if (body !== undefined) opts.body = JSON.stringify(body)
  const res = await fetch(`${getBase()}${path}`, opts)
  const data = await res.json().catch(() => null)
  if (!res.ok)
    throw new ModelsError(res.status, data?.message || `${method} ${path} → ${res.status}`)
  return data
}

const q = (revision) => `revision=${encodeURIComponent(revision)}`
const enc = encodeURIComponent

export const getModels = () => call('GET', '/models')
export const getCatalog = () => call('GET', '/models/catalog')
export const getDetected = () => call('GET', '/models/detected')
// Logs in to a provider not yet saved (free). Typed secrets are kept in
// secrets.toml by the daemon at once; the answer carries references only.
export const checkProvider = (name, connection, secrets = {}) =>
  call('POST', '/models/check', { name, connection, secrets })
export const providerModels = (name) => call('GET', `/models/providers/${enc(name)}/models`)
export const saveSetup = (body) => call('POST', '/models/setup', body)
export const setDefault = (revision, reference) =>
  call('PUT', '/models/default', { revision, reference })
export const setTasks = (revision, tasks, reference = null) =>
  call('PUT', '/models/tasks', { revision, tasks, reference })
export const addAlias = (revision, alias) => call('POST', `/models/aliases?${q(revision)}`, alias)
export const removeAlias = (revision, name) =>
  call('DELETE', `/models/aliases/${enc(name)}?${q(revision)}`)
export const removeProvider = (revision, name) =>
  call('DELETE', `/models/providers/${enc(name)}?${q(revision)}`)
export const getSessionModels = (sessionId) => call('GET', `/models/sessions/${enc(sessionId)}`)
export const setSessionModels = (sessionId, model, subagents = {}) =>
  call('PUT', `/models/sessions/${enc(sessionId)}`, { model, subagents })
// Billable: one short message. Only ever sent from an explicit click.
export const testModel = (reference) => call('POST', '/models/test', { reference })

// Every model a picker should offer: the aliases, then each reference the file
// names (default, tasks), once each.
export function modelChoices(status) {
  if (!status?.configured) return []
  const seen = []
  for (const ref of [
    ...status.models.map((m) => m.name),
    status.default,
    ...status.tasks.map((t) => t.reference),
  ]) {
    if (ref && !seen.includes(ref)) seen.push(ref)
  }
  return seen
}
