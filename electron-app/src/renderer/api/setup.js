// Setting YUYUTSAVA up, over the daemon's /setup endpoints.
//
// Its own module rather than more of client.js: these are the only calls that
// change the machine rather than the conversation, and keeping them together
// makes that visible. They are the same catalog, probes and compose commands
// `yuyutsava setup` uses in a terminal — one implementation, two surfaces.

import { getBase } from './client'

// The port comes from client.js's initBase(), which the main process reports at
// start-up. A second copy of the default here would be a second thing to get
// wrong the moment anyone changes YUYUTSAVA_DAEMON_PORT.
async function _json(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } }
  if (body !== undefined) opts.body = JSON.stringify(body)
  const res = await fetch(`${getBase()}${path}`, opts)
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status}`)
  return res.json()
}

// What is configured, what is missing, and what can be started. Runs no probe:
// a screen that cannot draw until a Postgres socket times out is worse than one
// that says "not checked yet".
export const getSetupState = () => _json('GET', '/setup/state')

// Which file settings are kept in, and what else they could be kept in.
export const getSetupSource = () => _json('GET', '/setup/source')

// Move every setting into settings.toml ('settings') or the .env ('env').
export const moveSettingsTo = (kind, keep = false) => _json('PUT', '/setup/source', { kind, keep })

// Write to whichever file is in force. An empty value removes a setting.
export const writeSettings = (values) => _json('PUT', '/setup/settings', { values })

// Run one probe. `live` really calls the provider and spends one search credit,
// so it is never passed unless the person asked for it.
export const runSetupCheck = (name, live = false) => _json('POST', `/setup/check/${name}`, { live })

// Start a container and wait until the thing inside it answers — not merely
// until docker returns, which is well before Postgres accepts a connection.
export const startSetupService = (name) => _json('POST', `/setup/services/${name}/start`)

// Start Docker Desktop, where there is one to start, and wait for its engine.
export const startDockerEngine = () => _json('POST', '/setup/engine/start')
