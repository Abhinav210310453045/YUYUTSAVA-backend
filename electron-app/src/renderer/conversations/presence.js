// Conversation-wide presence, read by surfaces that do not own a conversation:
// who is speaking (the titlebar transport) and which threads the user can see
// (ask routing). Sessions report into it; nothing here knows about sockets.

// ---------------------------------------------------------------------------
// Who is currently speaking — read by the titlebar transport button
// ---------------------------------------------------------------------------

let speaker = null
const speakerListeners = new Set()

export function subscribeSpeaker(fn) {
  speakerListeners.add(fn)
  return () => speakerListeners.delete(fn)
}
export function getSpeaker() {
  return speaker
}

export function setSpeaker(next) {
  if (speaker?.key === next?.key && speaker?.label === next?.label) return
  speaker = next
  for (const fn of speakerListeners) {
    try {
      fn(speaker)
    } catch {
      /* ignore */
    }
  }
}

// ---------------------------------------------------------------------------
// Which conversations the user can actually SEE right now
// ---------------------------------------------------------------------------
//
// Ask routing needs "am I looking at the conversation that owns this ask?" —
// which is not the same question as "am I on the Chat tab". Two different chats
// both live on the Chat panel, and a card can hold many tinker threads. Getting
// that wrong in the permissive direction leaks a prompt into someone else's
// session; getting it wrong the other way silently swallows the notification
// and the agent waits forever. So visibility is tracked per *thread*, reported
// by the panel that is rendering it.

const visibleThreads = new Map() // thread_id -> refcount
const visibleListeners = new Set()

export function subscribeVisibleThreads(fn) {
  visibleListeners.add(fn)
  return () => visibleListeners.delete(fn)
}

export function isThreadVisible(threadId) {
  return !!threadId && visibleThreads.has(threadId)
}

export function visibleThreadIds() {
  return [...visibleThreads.keys()]
}

export function setThreadVisible(threadId, visible) {
  if (!threadId) return
  const n = visibleThreads.get(threadId) || 0
  if (visible) visibleThreads.set(threadId, n + 1)
  else if (n <= 1) visibleThreads.delete(threadId)
  else visibleThreads.set(threadId, n - 1)
  for (const fn of visibleListeners) {
    try {
      fn()
    } catch {
      /* ignore */
    }
  }
}
