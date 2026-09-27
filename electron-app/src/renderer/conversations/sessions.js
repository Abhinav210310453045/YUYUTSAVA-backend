// The live conversations, one per key. Conversations outlive the components
// that show them: a panel acquires its session, retains it while mounted and
// releases it on unmount; the session keeps its socket while a turn runs and
// lets go once idle. This map caps how many stay around.

import { ConversationSession } from './session'

const MAX_SESSIONS = 24

const sessions = new Map()

export function conversationKey({
  origin = 'cli',
  agent = null,
  card = null,
  resumeId = null,
} = {}) {
  return `${origin}|${agent || ''}|${card || ''}|${resumeId || ''}`
}

export function acquireSession(key, opts) {
  let s = sessions.get(key)
  if (!s || s.disposed) {
    s = new ConversationSession(key, opts)
    s.onDispose = () => {
      if (sessions.get(key) === s) sessions.delete(key)
    }
    sessions.set(key, s)
    evictIdle()
  }
  return s
}

function evictIdle() {
  if (sessions.size <= MAX_SESSIONS) return
  const idle = [...sessions.values()]
    .filter((s) => s.refs === 0 && !s.state.busy)
    .sort((a, b) => a.touchedAt - b.touchedAt)
  for (const s of idle) {
    if (sessions.size <= MAX_SESSIONS) break
    s.dispose()
  }
}

// Test/debug affordance — not used by the app.
export function _sessionsForTest() {
  return sessions
}
