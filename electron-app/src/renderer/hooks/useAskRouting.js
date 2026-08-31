import { useCallback, useMemo, useSyncExternalStore } from 'react'
import { useNav } from '../nav/NavProvider'
import {
  isThreadVisible, subscribeVisibleThreads, visibleThreadIds,
} from '../conversations/store'

// useSyncExternalStore needs a snapshot that changes when visibility changes;
// the count of on-screen conversations is enough and is stable between changes.
const visibleThreadCount = () => visibleThreadIds().length

// Where an ask is allowed to render.
//
// The rule this file exists to enforce, in one sentence: **a permission prompt
// must never appear inside a different running session's path.** A pop-up asking
// to approve something is for a user who is busy elsewhere; dropping it into
// whatever chat happens to be open would make you approve one agent's action
// from inside another agent's conversation.
//
// So an ask belongs to exactly one owning surface (recorded server-side on the
// ask itself), and rendering is a pure function of (owner, where the user is):
//
//   on the owning view      → inline, and ONLY here
//   elsewhere in the app     → a notification naming that agent + an Inbox entry
//   app not focused          → the always-on-top overlay (never steals focus)
//   background task          → Inbox (+ overlay), never inline anywhere
//   always, while pending    → listed in the Inbox
//
// Every surface asks this module rather than deciding for itself, so there is
// exactly one place a leak could come from — and exactly one place to fix it.

/**
 * Does `ask` belong to the conversation identified by these coordinates?
 *
 * Matching is by thread first (the durable identity), falling back to
 * session_id for records written before a thread was pinned. A background ask
 * never matches a conversation, even when it was launched from one — it is the
 * task's ask, not the chat's.
 */
export function askOwnedByConversation(ask, { threadId, sessionId, cardId, surface } = {}) {
  if (!ask) return false
  if (ask.surface === 'background' || ask.task_id) return false
  const owner = ask.thread_id || ask.session_id
  if (!owner) return false
  if (threadId && owner === threadId) return true
  if (sessionId && owner === sessionId) return true
  // A tinker ask may arrive before the pane has learned its thread id; the card
  // it belongs to is enough to place it, as long as the surface agrees.
  if (cardId && ask.card_id === cardId && (!surface || ask.surface === surface)) return true
  return false
}

/**
 * The inline subset for one conversation view. Anything not owned here is
 * deliberately invisible to it.
 */
export function useInlineAsks(asks, coords) {
  const { threadId, sessionId, cardId, surface } = coords || {}
  return useMemo(
    () => (asks || []).filter((a) =>
      askOwnedByConversation(a, { threadId, sessionId, cardId, surface })),
    [asks, threadId, sessionId, cardId, surface],
  )
}

/** Where the user must go to answer `ask` inline. */
export function askDestination(ask) {
  if (!ask) return null
  switch (ask.surface) {
    case 'tinker':
      return ask.card_id
        ? { panel: 'todos', params: { cardId: ask.card_id, ...(ask.thread_id ? { chat: ask.thread_id } : {}) } }
        : { panel: 'proposals', params: {} }
    case 'voice':
      return { panel: 'voice', params: {} }
    case 'chat':
      return { panel: 'chat', params: {} }
    default:
      // Background / CLI asks have no inline home — the Inbox is where they live.
      return { panel: 'proposals', params: {} }
  }
}

/**
 * Routing for the app shell: which asks are "somewhere else" right now (so they
 * warrant a notification), and how to navigate to one.
 */
export function useAskRouting(asks) {
  const { activePanel, params, push, switchTab } = useNav()
  // Re-evaluate when the set of on-screen conversations changes (tab switch,
  // pane opened, chat switched).
  const visibleTick = useSyncExternalStore(subscribeVisibleThreads, visibleThreadCount)

  const isOnOwningView = useCallback((ask) => {
    // The question is "can the user SEE the conversation that owns this?" —
    // not "are they on the tab it lives on". Two chats share the Chat panel and
    // a card holds many tinker threads, so a panel match is not an ownership
    // match: answering that too loosely leaves an agent waiting with no
    // notification, because we wrongly assumed the inline card was in front of
    // them. The conversation store reports which threads are actually rendered.
    if (!ask) return false
    if (ask.surface === 'background' || ask.task_id) return false
    if (isThreadVisible(ask.thread_id) || isThreadVisible(ask.session_id)) return true
    // A tinker ask whose thread the pane hasn't resolved yet still counts as
    // visible when its card view is open.
    if (ask.surface === 'tinker' && ask.card_id) {
      return activePanel === 'todos' && params.cardId === ask.card_id
    }
    return false
    // visibleTick is a dependency on purpose: it is the change signal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePanel, params.cardId, visibleTick])

  const goToAsk = useCallback((ask) => {
    const dest = askDestination(ask)
    if (!dest) return
    if (dest.panel === 'todos') push('todos', dest.params)
    else switchTab(dest.panel)
  }, [push, switchTab])

  // Asks the user cannot currently see inline. These are the ones that need a
  // notification — and they are announced, never rendered, outside their owner.
  const elsewhere = useMemo(
    () => (asks || []).filter((a) => !isOnOwningView(a)),
    [asks, isOnOwningView],
  )

  return { isOnOwningView, goToAsk, elsewhere, askDestination }
}
