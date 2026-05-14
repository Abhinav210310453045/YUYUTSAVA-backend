import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { useSSE } from './useSSE.jsx'
import { useFocus } from './useFocus'

// Per the plan (PHASE_2_PLAN §4.4): only urgency>=2 proposals trigger a banner.
// Asks are always blocking, so they banner regardless.
const PROPOSAL_BANNER_MIN_URGENCY = 2
const TOAST_TTL_MS = 6000

const NotificationsContext = createContext(null)

export function NotificationsProvider({ children }) {
  const { proposals, asks } = useSSE()
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
    setToasts((prev) => [...prev, toast])
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== toast.id))
    }, TOAST_TTL_MS)
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

  // Route new asks — always banner when unfocused (blocking on user input).
  useEffect(() => {
    for (const [id, a] of asks) {
      if (seenAskIds.current.has(id)) continue
      seenAskIds.current.add(id)

      if (!focused) {
        window.electronAPI?.showNotification?.({
          title: a.title || 'Question',
          body: a.body || '',
          proposalId: id, // re-use the field; click handler scrolls to this id
          urgency: 3,
        })
      } else {
        pushToast({
          id: `a-${id}`,
          kind: 'ask',
          proposalId: id,
          title: a.title || 'Question',
          body: a.body || '',
        })
      }
    }
    for (const id of Array.from(seenAskIds.current)) {
      if (!asks.has(id)) seenAskIds.current.delete(id)
    }
  }, [asks, focused, pushToast])

  // Clear highlight after a short window so the same id can re-highlight later.
  useEffect(() => {
    if (highlightId == null) return
    const t = setTimeout(() => setHighlightId(null), 4000)
    return () => clearTimeout(t)
  }, [highlightId])

  return (
    <NotificationsContext.Provider value={{ toasts, dismissToast, highlightId }}>
      {children}
    </NotificationsContext.Provider>
  )
}

export function useNotifications() {
  return useContext(NotificationsContext)
}
