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
  if (res.status === 204) return null // no-body responses (todo DELETEs)
  return res.json()
}

export const respondProposal = (id, decision, editedInstruction = null) =>
  _json('POST', `/proposal/${id}/respond`, { decision, edited_instruction: editedInstruction })

export const respondAsk = (id, response) =>
  _json('POST', `/ask/${id}/respond`, { response })

// Every ask still awaiting an answer. Asks never expire and the SSE broadcast
// can drop frames, so this is how a surface (re)discovers what is pending —
// on connect, and after a daemon restart that left agents parked mid-interrupt.
export const getAsks = () => _json('GET', '/asks?status=pending')

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
// Phase 6b: resume-history + replay. messages = ordered chat turns; a turn
// with stored TTS carries its own audio_url (voice-store seq — do not rebuild
// it from the row seq), served as a WAV for the ▶ replay.
export const getSessionMessages = (id) =>
  _json('GET', `/sessions/${encodeURIComponent(id)}/messages`)

// Message feedback (👍/👎). Stores the reacted-to (user, assistant) pair for a
// future feedback agent; survives session deletion. Re-rating upserts.
export const submitFeedback = (body) => _json('POST', '/feedback', body)
export const listFeedback = (sessionId = null) =>
  _json('GET', sessionId ? `/feedback?session_id=${encodeURIComponent(sessionId)}` : '/feedback')

// Rendered visuals (charts/diagrams/tables/...) for the Artifacts panel.
// listVisuals returns metadata; visualUrl builds the absolute image URL the
// <img> tag (Artifacts grid + inline chat) points at.
export const listVisuals = (sessionId) =>
  _json('GET', `/sessions/${encodeURIComponent(sessionId)}/visuals`)
export const visualUrl = (url) => `${_base}${url}`
// Delete a visual everywhere the agent saved it (DB row + on-disk image). A
// copy the user downloaded via the Download button lives elsewhere and survives.
export const deleteVisual = (visualId) =>
  _json('DELETE', `/visuals/${encodeURIComponent(visualId)}`)

// General artifacts (artifact_create: interactive HTML/JSX, docs, audio) and
// every TODO card's attachments — the non-visual half of the Artifacts gallery.
// Each record carries `attachment_id`; artifacts resolve bytes from their `url`,
// card attachments from todoAttachmentUrl(card_id, attachment_id).
export const listArtifacts = (limit = 200) =>
  _json('GET', `/artifacts?limit=${limit}`)
export const listAllAttachments = (limit = 200) =>
  _json('GET', `/todos/attachments?limit=${limit}`)

// Full transcript of a background (async-subagent) task, fetched on demand
// (tool calls, results, and text) for the TASKS panel's expandable log view.
export const getTaskLogs = (taskId) =>
  _json('GET', `/tasks/${encodeURIComponent(taskId)}/logs`)

export const getLogLevel = () => _json('GET', '/logs/level')
export const setLogLevel = (level) => _json('PUT', '/logs/level', { level })

// TODO board (docs/TODO_BOARD_PLAN.md). Responses are the versioned exchange
// models (TodoCardV1 / TodoCardSummaryV1 / TodoNoteV1); note PATCH/DELETE are
// scoped under the card so the daemon can 404 cross-card note ids.
export const listTodos = (status = null, tag = null, limit = 500) => {
  const qs = new URLSearchParams()
  if (status) qs.set('status', status)
  if (tag) qs.set('tag', tag)
  qs.set('limit', String(limit))
  return _json('GET', `/todos?${qs}`)
}
export const createTodo = (title, { status, tags, pinned, note } = {}) =>
  _json('POST', '/todos', { title, status, tags, pinned, note })
export const getTodo = (cardId) => _json('GET', `/todos/${encodeURIComponent(cardId)}`)
// Partial update — pass only the fields to change (title/status/pinned/tags).
export const patchTodo = (cardId, fields) =>
  _json('PATCH', `/todos/${encodeURIComponent(cardId)}`, fields)
export const deleteTodo = (cardId) => _json('DELETE', `/todos/${encodeURIComponent(cardId)}`)
export const addTodoNote = (cardId, body, author = 'user', { objectiveId = null, phase = null } = {}) =>
  _json('POST', `/todos/${encodeURIComponent(cardId)}/notes`,
    { body, author, objective_id: objectiveId, phase })
export const patchTodoNote = (cardId, noteId, body) =>
  _json('PATCH', `/todos/${encodeURIComponent(cardId)}/notes/${encodeURIComponent(noteId)}`, { body })
export const deleteTodoNote = (cardId, noteId) =>
  _json('DELETE', `/todos/${encodeURIComponent(cardId)}/notes/${encodeURIComponent(noteId)}`)
// Move a note onto an objective (objectiveId=null clears the assignment).
export const assignTodoNote = (cardId, noteId, objectiveId, phase = null) =>
  _json('PATCH', `/todos/${encodeURIComponent(cardId)}/notes/${encodeURIComponent(noteId)}/assign`,
    { objective_id: objectiveId, phase })

// Think-flow objectives: a card's structured decomposition. phase is one of
// thinking/planning/doing/completed/blocked/abandoned; PATCH is partial.
export const addTodoObjective = (cardId, title, phase = 'thinking') =>
  _json('POST', `/todos/${encodeURIComponent(cardId)}/objectives`, { title, phase })
export const patchTodoObjective = (cardId, objectiveId, fields) =>
  _json('PATCH', `/todos/${encodeURIComponent(cardId)}/objectives/${encodeURIComponent(objectiveId)}`, fields)
export const deleteTodoObjective = (cardId, objectiveId) =>
  _json('DELETE', `/todos/${encodeURIComponent(cardId)}/objectives/${encodeURIComponent(objectiveId)}`)
// The card's activity timeline (oldest first) — feeds the Activity strip.
export const listTodoEvents = (cardId, limit = 500) =>
  _json('GET', `/todos/${encodeURIComponent(cardId)}/events?limit=${limit}`)
// The card's tinker chat sessions, newest first (session rows: id, title,
// task_preview, updated_at, message_count, …).
export const listTodoChats = (cardId, limit = 50) =>
  _json('GET', `/todos/${encodeURIComponent(cardId)}/chats?limit=${limit}`)
// One-step generate+attach via a generative block ('journey', 'diagram', 'audio').
export const generateTodoArtifact = (cardId, block, spec = {}, title = null) =>
  _json('POST', `/todos/${encodeURIComponent(cardId)}/generate`, { block, spec, title })

// TODO attachments (Phase 4). Upload is multipart FormData — no Content-Type
// header, the browser sets the boundary. Rejections (415 mime / 413 size)
// carry a human `detail` worth surfacing, unlike the generic _json errors.
export async function uploadTodoAttachment(cardId, file, { title, kind, objectiveId, noteId } = {}) {
  const form = new FormData()
  form.append('file', file)
  if (title) form.append('title', title)
  if (kind) form.append('kind', kind)
  // Soft association: the row stays card-level, the pointer rides meta.
  if (objectiveId) form.append('objective_id', objectiveId)
  if (noteId) form.append('note_id', noteId)
  const res = await fetch(`${_base}/todos/${encodeURIComponent(cardId)}/attachments`, {
    method: 'POST',
    body: form,
  })
  if (!res.ok) {
    let detail = ''
    try { detail = (await res.json()).detail || '' } catch { /* non-JSON body */ }
    throw new Error(detail || `upload → ${res.status}`)
  }
  return res.json()
}
// Serves the attachment's bytes (images render it in <img>; download=true adds
// Content-Disposition so the browser saves instead of displays). `v` is a
// cache-buster: singleton blocks (journey) regenerate under the SAME
// attachment id/URL, so pass the row's created_ts to defeat stale HTTP cache.
export const todoAttachmentUrl = (cardId, attachmentId, { download = false, v = null } = {}) => {
  const qs = [download ? 'download=true' : null, v != null ? `v=${encodeURIComponent(v)}` : null]
    .filter(Boolean).join('&')
  return `${_base}/todos/${encodeURIComponent(cardId)}/attachments/${encodeURIComponent(attachmentId)}${qs ? `?${qs}` : ''}`
}
export const deleteTodoAttachment = (cardId, attachmentId) =>
  _json('DELETE', `/todos/${encodeURIComponent(cardId)}/attachments/${encodeURIComponent(attachmentId)}`)

// Percent-encode each segment but keep the separators — a bundle rel_path may be
// nested, and filenames routinely carry spaces ("Face Rig.dc.html").
const _relPath = (rel) => String(rel).split('/').map(encodeURIComponent).join('/')

// Serves any file from the attachment's OWN directory. Framing the primary file
// here (rather than inlining its bytes) is what lets a multi-file artifact's
// relative refs — `./support.js` and friends — resolve. See web/bundle.py.
export const todoAttachmentBundleUrl = (cardId, attachmentId, relPath) =>
  `${_base}/todos/${encodeURIComponent(cardId)}/attachments/${encodeURIComponent(attachmentId)}/bundle/${_relPath(relPath)}`

// General (non-card) artifacts produced by the master/tinker for inline chat &
// voice. The record carries a relative `url` (/artifacts/{id}); this builds the
// absolute URL the block components fetch/render, the twin of visualUrl.
export const artifactUrl = (url) => (url ? `${_base}${url}` : url)

// The general-artifact twin of todoAttachmentBundleUrl (keyed by artifact_id,
// which the wire record aliases onto `attachment_id`).
export const artifactBundleUrl = (artifactId, relPath) =>
  `${_base}/artifacts/${encodeURIComponent(artifactId)}/bundle/${_relPath(relPath)}`

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
  const res = await patchEventsConfig(sources)
  // The runtime toggle is the OWNER of the voice source's enabled bit (the
  // daemon re-applies it on every reload and on boot). Turning the source on
  // here without it would be undone the next time the config reloads.
  await patchRuntimeSettings({ voice: { wake_enabled: true } }).catch(() => {})
  return res
}

// Hot runtime toggles (voice mode, dedicated subagents). Distinct from
// /config/* above: these apply immediately, need no restart, and are broadcast
// to every surface as a `settings` SSE item — see useRuntimeSettings.
export const getRuntimeSettings = () => _json('GET', '/settings/runtime')
export const patchRuntimeSettings = (patch) => _json('PATCH', '/settings/runtime', patch)
// The dedicated subagent roster (name/description/enabled), built from the
// agents the daemon actually booted with — a new subagent needs no UI change.
export const getSubagentRoster = () => _json('GET', '/settings/subagents')
