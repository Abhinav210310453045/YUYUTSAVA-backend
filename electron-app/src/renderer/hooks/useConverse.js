import { useEffect, useMemo, useSyncExternalStore } from 'react'
import { acquireSession, conversationKey } from '../conversations/store'

// A view onto a conversation — not the owner of one.
//
// The conversation itself (socket, messages, busy state, mic, playback) lives
// in conversations/store.js, keyed so that every mount of the same chat lands
// on the same session. Unmounting this hook merely releases it: a turn already
// running keeps streaming into the store, and remounting picks it straight back
// up with nothing lost. The daemon, in turn, owns the *run* (see
// yuyutsava/daemon/turn_registry.py) — so neither the component nor the socket
// can end a turn any more. Only the Stop button does.
//
// `agent`/`card` select the server-side bundle (agent='tinker' + a card id
// pins the thread to that TODO card); omitted → the master deepagent.
//
// `sessionKey` overrides the derived identity. Hosts that can show several
// conversations at the same coordinates need it: the TODO card view's "New
// chat" produces another chat with the same origin/agent/card and no resumeId
// yet, and only the host knows those are meant to be different conversations.
export function useConverse({
  origin = 'cli', resumeId = null, agent = null, card = null, sessionKey = null,
  // Whether the panel showing this conversation is actually on screen. Ask
  // routing depends on it: an ask renders inline only where the user can see
  // it, and "on the Chat tab" is not the same as "looking at THIS chat".
  active = true,
} = {}) {
  const key = sessionKey || conversationKey({ origin, agent, card, resumeId })
  const session = useMemo(
    () => acquireSession(key, { origin, resumeId, agent, card }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key],
  )

  useEffect(() => {
    session.retain()
    return () => session.release()
  }, [session])

  useEffect(() => {
    session.setVisible(active)
    return () => session.setVisible(false)
  }, [session, active])

  const state = useSyncExternalStore(session.subscribe, session.getSnapshot)

  return useMemo(() => ({ ...state, ...session.actions }), [state, session])
}
