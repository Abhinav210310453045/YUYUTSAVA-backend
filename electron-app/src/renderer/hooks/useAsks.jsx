import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getAsks, respondAsk } from '../api/client'
import { SSEClient } from '../api/sse'
import { useSSE } from './useSSE.jsx'

// The single source of truth for asks awaiting an answer.
//
// An ask is the one thing allowed to block an agent indefinitely, so it can
// never be something a client merely happened to see: `GET /asks` hydrates on
// connect (SSE frames can be dropped, and a daemon restart has no live
// broadcast at all), live `ask`/`ask_resolved` frames keep it current, and an
// answer removes it optimistically so the card doesn't linger while the POST
// is in flight.
//
// Everything here is ownership-agnostic — WHERE an ask may render is decided in
// exactly one place, useAskRouting.js.

function sortAsks(map) {
  // Oldest first: the ask that has been blocking longest is the one to answer.
  return [...map.values()].sort(
    (a, b) => (a.created_ts || 0) - (b.created_ts || 0),
  )
}

// Shared answer path: optimistic removal, then the POST. On failure the ask is
// put back — a card that silently vanished without the agent being unblocked
// would be the worst possible outcome here.
function useAnswer(remove, restore) {
  const [answering, setAnswering] = useState(null) // ask_id currently in flight

  return {
    answering,
    answer: useCallback(async (ask, response) => {
      const id = ask?.ask_id
      if (!id) return false
      setAnswering(id)
      remove(id)
      try {
        await respondAsk(id, response)
        return true
      } catch (e) {
        console.warn('answering ask failed:', e)
        restore(ask)
        return false
      } finally {
        setAnswering(null)
      }
    }, [remove, restore]),
  }
}

/**
 * Main-window asks: the app already runs one SSE stream (SSEProvider), so this
 * layers hydration and answering on top of it rather than opening a second.
 */
export function useAsks() {
  const { asks: liveAsks, connected, removeAsk } = useSSE()
  // Hydrated rows live alongside the SSE map until a frame supersedes them.
  const [hydrated, setHydrated] = useState(() => new Map())
  const wasConnected = useRef(false)

  useEffect(() => {
    // Hydrate on every (re)connect: whatever we missed while the stream was
    // down — including asks raised by a previous daemon process — shows up here.
    if (!connected || wasConnected.current) {
      wasConnected.current = connected
      return
    }
    wasConnected.current = true
    let cancelled = false
    getAsks()
      .then((res) => {
        if (cancelled) return
        setHydrated(new Map((res?.asks || []).map((a) => [a.ask_id, a])))
      })
      .catch(() => { /* the SSE stream is still the live path */ })
    return () => { cancelled = true }
  }, [connected])

  const merged = useMemo(() => {
    const m = new Map(hydrated)
    for (const [id, a] of liveAsks) m.set(id, a)   // live frames win
    return m
  }, [hydrated, liveAsks])

  const remove = useCallback((id) => {
    removeAsk(id)
    setHydrated((prev) => {
      if (!prev.has(id)) return prev
      const next = new Map(prev)
      next.delete(id)
      return next
    })
  }, [removeAsk])

  const restore = useCallback((ask) => {
    setHydrated((prev) => new Map(prev).set(ask.ask_id, ask))
  }, [])

  const { answer, answering } = useAnswer(remove, restore)
  const asks = useMemo(() => sortAsks(merged), [merged])

  return { asks, count: asks.length, answer, answering, dismiss: remove }
}

/**
 * Asks in a renderer with no SSEProvider — the always-on-top overlay, which is
 * its own process. Deliberately a *lean* subscription: mounting the full
 * SSEProvider here would duplicate its side effects (wake-word forwarding, OS
 * notifications, the tray badge) and, in the wake case, pop the overlay at
 * itself in a loop.
 */
export function useStandaloneAsks() {
  const [asksMap, setAsksMap] = useState(() => new Map())

  useEffect(() => {
    let cancelled = false
    const upsert = (a) => setAsksMap((prev) => new Map(prev).set(a.ask_id, a))
    const drop = (id) => setAsksMap((prev) => {
      if (!prev.has(id)) return prev
      const next = new Map(prev)
      next.delete(id)
      return next
    })

    const hydrate = () => {
      getAsks()
        .then((res) => {
          if (cancelled) return
          setAsksMap((prev) => {
            const next = new Map(prev)
            for (const a of res?.asks || []) next.set(a.ask_id, a)
            return next
          })
        })
        .catch(() => { /* best effort */ })
    }

    const client = new SSEClient({
      onConnected: hydrate,
      onAsk: (data) => { if (data?.ask) upsert(data.ask) },
      onAskResolved: (data) => drop(data?.ask_id),
    })
    client.connect()
    hydrate()
    return () => { cancelled = true; client.disconnect() }
  }, [])

  const remove = useCallback((id) => setAsksMap((prev) => {
    if (!prev.has(id)) return prev
    const next = new Map(prev)
    next.delete(id)
    return next
  }), [])
  const restore = useCallback((ask) => {
    setAsksMap((prev) => new Map(prev).set(ask.ask_id, ask))
  }, [])

  const { answer, answering } = useAnswer(remove, restore)
  const asks = useMemo(() => sortAsks(asksMap), [asksMap])

  return { asks, count: asks.length, answer, answering, dismiss: remove }
}
