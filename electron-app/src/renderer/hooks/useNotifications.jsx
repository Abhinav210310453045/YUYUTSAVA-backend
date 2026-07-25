import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { useSSE } from './useSSE.jsx'
import { useAsks } from './useAsks.jsx'
import { useAskRouting } from './useAskRouting'
import { useFocus } from './useFocus'

// Per the plan (PHASE_2_PLAN §4.4): only urgency>=2 proposals trigger a banner.
// Asks are always blocking, so they banner regardless.
const PROPOSAL_BANNER_MIN_URGENCY = 2
const TOAST_TTL_MS = 6000
// An ask that nobody can see inline is not a passing notice — it stays until
// it's dealt with, because an agent is blocked on it.
const ASK_TOAST_TTL_MS = 20000

const NotificationsContext = createContext(null)

export function NotificationsProvider({ children }) {
  const { proposals } = useSSE()
  const { asks } = useAsks()
  const { isOnOwningView, goToAsk } = useAskRouting(asks)
  const focused = useFocus()

  // Track which IDs we've already routed so re-renders don't fire duplicates.
  const seenProposalIds = useRef(new Set())
  const seenAskIds = useRef(new Set())

  // In-window toasts shown when the window is focused.
  const [toasts, setToasts] = useState([])
  // The proposalId the user just clicked in an OS banner — ProposalsPanel
  // listens for changes and scrolls/highlights.
  const [highlightId, setHighlightId] = useState(null)

  const pushToast = useCallback((toast) => {
    setToasts((prev) => [...prev.filter((t) => t.id !== toast.id), toast])
    const ttl = toast.ttl || TOAST_TTL_MS
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== toast.id))
    }, ttl)
  }, [])

  const dismissToast = useCallback((id) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  // Subscribe to OS banner clicks — set highlightId and let panels react.
  useEffect(() => {
    const off = window.electronAPI?.onNotificationClick?.(({ proposalId }) => {
      setHighlightId(proposalId)
    })
    return () => off && off()
  }, [])

  // Route new proposals.
  useEffect(() => {
    for (const [id, p] of proposals) {
      if (seenProposalIds.current.has(id)) continue
      seenProposalIds.current.add(id)

      const shouldBanner = (p.urgency ?? 0) >= PROPOSAL_BANNER_MIN_URGENCY
      if (!focused && shouldBanner) {
        window.electronAPI?.showNotification?.({
          title: p.summary || 'New proposal',
          body: p.proposed || '',
          proposalId: id,
          urgency: p.urgency,
        })
      } else {
        pushToast({
          id: `p-${id}`,
          kind: 'proposal',
          proposalId: id,
          title: p.summary || 'Proposal',
          body: p.proposed || '',
        })
      }
    }
    // Forget IDs that left the map so re-arriving proposals (rare) re-notify.
    for (const id of Array.from(seenProposalIds.current)) {
      if (!proposals.has(id)) seenProposalIds.current.delete(id)
    }
  }, [proposals, focused, pushToast])

  // Route new asks.
  //
  // The rule that matters here: an ask is *announced* everywhere but *rendered*
  // only on the view that owns it. If the user is already looking at that view
  // the inline card is right in front of them and a toast is noise — so we stay
  // quiet. Anywhere else they get a pointer ("TinkerAgent needs permission →
  // Open") which navigates; it never carries the decision itself, because
  // approving one session's action from inside another's is exactly the leak
  // this design exists to prevent. Unfocused, the always-on-top overlay (main
  // process) takes over, so we only fire the OS banner alongside it.
  useEffect(() => {
    const live = new Set()
    for (const ask of asks) {
      const id = ask.ask_id
      live.add(id)
      if (seenAskIds.current.has(id)) continue
      seenAskIds.current.add(id)

      if (isOnOwningView(ask)) continue   // the inline card is already visible

      const who = ask.agent_label || 'An agent'
      if (!focused) {
        // The user is in another app. Summon the always-on-top overlay — the
        // only surface that can reach them there — and back it with an OS
        // banner. This has to be driven from HERE: the overlay window is
        // created lazily, so the AskOverlay living inside it cannot be the
        // thing that pops it. Main decides the rest (and never steals focus).
        window.electronAPI?.showAskOverlay?.({
          ask_id: id,
          title: ask.title || '',
          agent: who,
        })
        window.electronAPI?.showNotification?.({
          title: `${who} needs your permission`,
          body: ask.title || ask.body || '',
          proposalId: id, // re-uses the field; the click handler scrolls to it
          urgency: 3,
        })
      } else {
        pushToast({
          id: `a-${id}`,
          kind: 'ask',
          proposalId: id,
          askId: id,
          title: `${who} needs your permission`,
          body: ask.title || '',
          ttl: ASK_TOAST_TTL_MS,
          action: { label: 'Open', run: () => goToAsk(ask) },
        })
      }
    }
    for (const id of Array.from(seenAskIds.current)) {
      if (!live.has(id)) {
        seenAskIds.current.delete(id)
        // Answered elsewhere — drop any pointer still on screen.
        setToasts((prev) => prev.filter((t) => t.askId !== id))
      }
    }
  }, [asks, focused, isOnOwningView, goToAsk, pushToast])

  // Clear highlight after a short window so the same id can re-highlight later.
  useEffect(() => {
    if (highlightId == null) return
    const t = setTimeout(() => setHighlightId(null), 4000)
    return () => clearTimeout(t)
  }, [highlightId])

  return (
    <NotificationsContext.Provider
      value={{ toasts, pushToast, dismissToast, highlightId }}
    >
      {children}
    </NotificationsContext.Provider>
  )
}

export function useNotifications() {
  return useContext(NotificationsContext)
}
