// The message list a conversation renders, as pure functions: what each kind of
// row looks like, and how the list and the ask queue change. No sockets, no
// timers, no audio — a session calls these and publishes the result.

let _mid = 0
export const nextId = () => `m${++_mid}`

// The server composes board-selection references into the stored user text;
// the user only ever typed what follows it.
const SELECTION_RE = /^<selection-context>\n[\s\S]*?\n<\/selection-context>\n\n/
export const stripSelection = (t) => (t || '').replace(SELECTION_RE, '')

// ---- rows -------------------------------------------------------------

export const userMessage = (text) => ({ id: nextId(), role: 'user', text, events: [] })

export const errorMessage = (text) => ({
  id: nextId(),
  role: 'assistant',
  error: true,
  events: [],
  text,
})

// A standalone row for something the user must see with no bubble to hang it
// on. The `role` keeps it out of the conversation proper — it is not something
// the agent said. `code` names the situation and `actions` what the user can do
// about it ([{ id, label }], see noticeActions.js); both come from the daemon.
export const noticeMessage = (text, level = 'info', { code = null, actions = [] } = {}) => ({
  id: nextId(),
  role: 'notice',
  level,
  text,
  code,
  actions: Array.isArray(actions) ? actions : [],
  events: [],
  images: [],
  artifacts: [],
  streaming: false,
})

export const streamingMessage = (id) => ({
  id,
  role: 'assistant',
  text: '',
  events: [],
  images: [],
  artifacts: [],
  streaming: true,
})

// ---- the list ---------------------------------------------------------

export const patchMessage = (messages, id, patch) =>
  messages.map((m) => (m.id === id ? patch(m) : m))

// The newest assistant block, streaming or just closed. `tool_call` closes the
// block it belongs to, so its result and logs have to attach to the last one
// rather than conjuring a fresh empty bubble.
export function lastAssistantId(messages) {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === 'assistant') return messages[i].id
  }
  return null
}

// Whether a user turn the daemon echoes (`turn_start`) is already on screen:
// we sent it ourselves, the voice path echoed the transcript, or hydration
// raced ahead of the replay. "On screen" = the newest user bubble says the same
// and nothing has answered it yet.
export function alreadyShown(messages, text) {
  let lastUser = -1
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'user') {
      lastUser = i
      break
    }
  }
  if (lastUser < 0 || messages[lastUser].text !== text) return false
  return !messages.slice(lastUser + 1).some((m) => m.role === 'assistant' && !m.streaming)
}

// A resumed thread's past turns, from the history endpoint. Voice turns carry
// an audio_url for ▶ replay; text chats are prose only.
export function fromHistory(history, base) {
  return (history?.messages || []).map((m) => ({
    id: nextId(),
    role: m.role,
    // The verbatim transcript stores the server-composed selection block ahead
    // of what the user typed — strip it so resumed threads match the live view.
    text: m.role === 'user' ? stripSelection(m.text) : m.text || '',
    events: [],
    // Inline artifact cards made during the turn — rebuilt server-side from the
    // transcript's artifact_create tool results.
    artifacts: m.artifacts || [],
    // The server's audio_url is authoritative: on mixed text+voice threads it
    // carries the VOICE-store seq, which differs from this row's transcript seq.
    audioUrl: m.audio_url ? `${base}${m.audio_url}` : null,
  }))
}

// ---- the ask queue ------------------------------------------------------
//
// A queue, not a slot: a second question used to silently replace the first,
// and the overwritten one went on blocking its agent.

export const withAsk = (asks, ask) => [...asks.filter((a) => a.ask_id !== ask.ask_id), ask]

// No id means "the turn ended" — everything parked on it is moot.
export const withoutAsk = (asks, askId) => (askId ? asks.filter((a) => a.ask_id !== askId) : [])
