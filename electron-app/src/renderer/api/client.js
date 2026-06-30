let _base = 'http://127.0.0.1:7654'

export async function initBase() {
  try {
    const port = await window.electronAPI.getDaemonPort()
    _base = `http://127.0.0.1:${port}`
  } catch {
    // running outside Electron (browser dev) — keep default
  }
}

export function getBase() { return _base }

async function _json(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } }
  if (body !== undefined) opts.body = JSON.stringify(body)
  const res = await fetch(`${_base}${path}`, opts)
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status}`)
  return res.json()
}

export const respondProposal = (id, decision, editedInstruction = null) =>
  _json('POST', `/proposal/${id}/respond`, { decision, edited_instruction: editedInstruction })

export const respondAsk = (id, response) =>
  _json('POST', `/ask/${id}/respond`, { response })

export const getRules = () => _json('GET', '/rules')
export const deleteRule = (id) => _json('DELETE', `/rules/${id}`)
export const getDecisions = (limit = 50) => _json('GET', `/decisions?limit=${limit}`)
export const getSkills = () => _json('GET', '/skills')
export const deleteSkill = (name) => _json('DELETE', `/skills/${name}`)

export const listSessions = (workspace = null, limit = 50, cursor = null, origin = null) => {
  const qs = new URLSearchParams()
  if (workspace) qs.set('workspace', workspace)
  qs.set('limit', String(limit))
  if (cursor != null) qs.set('cursor', String(cursor))
  if (origin) qs.set('origin', origin)
  return _json('GET', `/sessions?${qs}`)
}
export const getSession = (id) => _json('GET', `/sessions/${encodeURIComponent(id)}`)
export const deleteSession = (id) => _json('DELETE', `/sessions/${encodeURIComponent(id)}`)
// Phase 6b: resume-history + replay. messages = ordered chat turns (text +
// audio_url for voice turns); the audio URL serves a WAV for the ▶ replay.
export const getSessionMessages = (id) =>
  _json('GET', `/sessions/${encodeURIComponent(id)}/messages`)
export const sessionAudioUrl = (id, seq) =>
  `${_base}/sessions/${encodeURIComponent(id)}/audio/${seq}`

export const getLogLevel = () => _json('GET', '/logs/level')
export const setLogLevel = (level) => _json('PUT', '/logs/level', { level })

// Config-variable catalog for the Settings UI (grouped/typed; reload_class
// per var). Served by the daemon so the form never drifts from the backend.
export const getConfigSchema = () => _json('GET', '/config/schema')

// Events config (sources map). PATCH replaces the whole sources map and hot
// reloads the affected sources.
export const getEventsConfig = () => _json('GET', '/config/events')
export const patchEventsConfig = (sources) => _json('PATCH', '/config/events', { sources })

// Enable the wake-word ("voice") events source with the chosen wake word,
// merging into the current sources map so other sources are preserved. The
// wake params hot-apply on reload (no daemon restart needed). Returns the new
// events config.
export async function enableVoiceSource(wakeWords, wakeThreshold = null) {
  const cfg = await getEventsConfig().catch(() => ({ sources: {} }))
  const sources = {}
  for (const [name, src] of Object.entries(cfg.sources || {})) {
    sources[name] = { enabled: src.enabled, params: { ...(src.params || {}) } }
  }
  const prev = sources.voice || { enabled: false, params: {} }
  const params = { ...(prev.params || {}) }
  if (wakeWords) params.wake_words = wakeWords
  if (wakeThreshold != null && wakeThreshold !== '') params.wake_threshold = wakeThreshold
  sources.voice = { enabled: true, params }
  return patchEventsConfig(sources)
}
