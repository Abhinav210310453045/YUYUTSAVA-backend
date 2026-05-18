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

export const listSessions = (workspace = null, limit = 50, cursor = null) => {
  const qs = new URLSearchParams()
  if (workspace) qs.set('workspace', workspace)
  qs.set('limit', String(limit))
  if (cursor != null) qs.set('cursor', String(cursor))
  return _json('GET', `/sessions?${qs}`)
}
export const getSession = (id) => _json('GET', `/sessions/${encodeURIComponent(id)}`)
export const deleteSession = (id) => _json('DELETE', `/sessions/${encodeURIComponent(id)}`)

export const getLogLevel = () => _json('GET', '/logs/level')
export const setLogLevel = (level) => _json('PUT', '/logs/level', { level })
