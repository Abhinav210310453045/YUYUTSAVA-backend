import { useCallback, useEffect, useRef, useState } from 'react'
import { useNav } from './NavProvider'

// Route-scoped view state: `useState` that outlives unmount.
//
// Panels are unmounted when you switch tabs, which is what destroys "where I
// was" — the open drawer, the selected rows, the half-typed title. Route
// params (NavProvider) carry *identity*; this carries everything else. Values
// live in a module-level cache keyed by scope, so remounting the same view
// picks them straight back up.
//
// Scope defaults to the active PANEL, deliberately not the routeKey: params
// change as you drill in (open an artifact, switch tinker chat) and panel-level
// state must not reset underneath that. Pass an explicit scope for state that
// belongs to one particular thing — TodoCardView keys on the card id, so two
// cards keep separate drawers and selections. Components that stay mounted
// while hidden (Chat, Voice) should always pass an explicit scope, since the
// active panel isn't theirs.
//
// GC is a plain LRU rather than lifetime tracking: entries are tiny (flags,
// id sets, short drafts) and evicting the wrong one only costs a reset drawer,
// whereas dropping state a user still wants is the bug we're fixing.

const CACHE_LIMIT = 60
const cache = new Map() // scope -> Map(slot -> value); insertion order = LRU

function touch(scope) {
  const slots = cache.get(scope)
  if (slots) { cache.delete(scope); cache.set(scope, slots) }
  return slots
}

function readViewState(scope, slot, initial) {
  const slots = touch(scope)
  if (slots && slots.has(slot)) return slots.get(slot)
  return typeof initial === 'function' ? initial() : initial
}

function writeViewState(scope, slot, value) {
  let slots = touch(scope)
  if (!slots) {
    slots = new Map()
    cache.set(scope, slots)
    while (cache.size > CACHE_LIMIT) cache.delete(cache.keys().next().value)
  }
  slots.set(slot, value)
}

// Explicit teardown for state whose subject is genuinely gone (a deleted card,
// a discarded chat) — the LRU would get there eventually, this is immediate.
export function dropViewState(scope) {
  cache.delete(scope)
}

export function useViewState(slot, initial, scopeOverride) {
  const nav = useNav()
  const scope = scopeOverride || nav.activePanel

  const [entry, setEntry] = useState(() => ({ scope, value: readViewState(scope, slot, initial) }))
  // Scope changed under us (navigated to a different card, say) — swap to that
  // scope's value during render rather than in an effect, so the very first
  // paint after the change already shows the right state.
  if (entry.scope !== scope) setEntry({ scope, value: readViewState(scope, slot, initial) })

  const valueRef = useRef(entry.value)
  valueRef.current = entry.value
  const scopeRef = useRef(scope)
  scopeRef.current = scope

  const set = useCallback((next) => {
    const value = typeof next === 'function' ? next(valueRef.current) : next
    writeViewState(scopeRef.current, slot, value)
    setEntry({ scope: scopeRef.current, value })
  }, [slot])

  return [entry.scope === scope ? entry.value : readViewState(scope, slot, initial), set]
}

// Scroll restoration for a panel's scroll container. Returns a ref to spread
// onto it. `ready` should flip true once the content that determines the
// scroll height has loaded — restoring before then would clamp to 0.
export function useScrollRestore(ready = true, scopeOverride, slot = 'scrollTop') {
  const nav = useNav()
  const scope = scopeOverride || nav.activePanel
  const ref = useRef(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return undefined
    if (ready) {
      const saved = readViewState(scope, slot, 0)
      if (saved) el.scrollTop = saved
    }
    // rAF-coalesced so a fast scroll doesn't write on every frame's event.
    let raf = 0
    const onScroll = () => {
      if (raf) return
      raf = requestAnimationFrame(() => { raf = 0; writeViewState(scope, slot, el.scrollTop) })
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      el.removeEventListener('scroll', onScroll)
      if (raf) cancelAnimationFrame(raf)
    }
  }, [scope, slot, ready])

  return ref
}
