import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

// App navigation: one back stack per tab (iOS tab-bar model), not a single
// global history. Switching tabs never counts as "back" — each tab remembers
// how deep you were in it, and the back arrow only unwinds depth within the
// tab you're looking at. That's what makes "go to Settings and come back"
// land you exactly where you left instead of at the tab's home screen.
//
// A Route is { panel, params } and params MUST be JSON-serializable scalars:
// the whole tree is persisted, and routeKey() derives view-state scopes from
// it (see useViewState.js). Params carry *identity* only — which card, which
// artifact. Everything else (drawer open? which rows selected?) is view state.

export const PANELS = ['proposals', 'sessions', 'todos', 'artifacts', 'settings', 'chat', 'voice']

const NAV_KEY = 'yy.nav.v1'
const RUN_ID_KEY = 'yy.app.runId'
// Chat is home on a fresh launch; an in-run reload restores the saved tree.
const HOME = 'chat'

const rootRoute = (panel) => ({ panel, params: {} })

function emptyStacks() {
  const stacks = {}
  for (const p of PANELS) stacks[p] = [rootRoute(p)]
  return stacks
}

// Stable, order-independent serialization so a routeKey doesn't change just
// because params were built in a different order.
function serializeParams(params) {
  const keys = Object.keys(params || {}).filter((k) => params[k] !== undefined && params[k] !== null).sort()
  return keys.map((k) => `${k}=${String(params[k])}`).join('&')
}

export function routeKey(route) {
  if (!route) return ''
  const q = serializeParams(route.params)
  return q ? `${route.panel}?${q}` : route.panel
}

// Merge patch into params, treating null/undefined as "remove this key" so
// callers can clear a param with replace({ chat: null }).
function mergeParams(base, patch) {
  const next = { ...(base || {}) }
  for (const [k, v] of Object.entries(patch || {})) {
    if (v === null || v === undefined) delete next[k]
    else next[k] = v
  }
  return next
}

function isValidTree(tree) {
  if (!tree || typeof tree !== 'object') return false
  if (!PANELS.includes(tree.activePanel)) return false
  if (!tree.stacks || typeof tree.stacks !== 'object') return false
  for (const p of PANELS) {
    const st = tree.stacks[p]
    if (!Array.isArray(st) || st.length === 0) return false
    if (!st.every((r) => r && r.panel === p && typeof r.params === 'object')) return false
  }
  return true
}

const NavContext = createContext(null)

export function useNav() {
  const ctx = useContext(NavContext)
  if (!ctx) throw new Error('useNav() must be used inside <NavProvider>')
  return ctx
}

export function NavProvider({ children }) {
  const [tree, setTree] = useState(() => ({ activePanel: HOME, stacks: emptyStacks() }))
  // Persistence is armed only once the restore attempt has settled, so the
  // default tree can't overwrite a good saved one during the async run-id
  // check. State, not a ref, so arming itself re-runs the persist effect —
  // otherwise a navigation that landed during the check would never be saved.
  const [armed, setArmed] = useState(false)
  // Set by the first navigation. If the user moved before the async restore
  // landed, the restore is dropped rather than yanking them somewhere else.
  const touched = useRef(false)

  // Fresh launch vs in-run reload: the main process mints one run id per
  // process, so a matching stored id means this boot is a renderer reload
  // (daemon restart, crash recovery) and the nav tree should come back.
  useEffect(() => {
    let cancelled = false
    Promise.resolve(window.electronAPI?.getAppRunId?.()).then((runId) => {
      if (cancelled) return
      try {
        if (runId && localStorage.getItem(RUN_ID_KEY) === String(runId)) {
          const saved = JSON.parse(localStorage.getItem(NAV_KEY) || 'null')
          if (isValidTree(saved) && !touched.current) setTree(saved)
        } else if (runId) {
          localStorage.setItem(RUN_ID_KEY, String(runId))
          localStorage.removeItem(NAV_KEY)
        }
      } catch { /* corrupt or unavailable storage — start at home */ }
      setArmed(true)
    }).catch(() => setArmed(true))
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!armed) return
    try { localStorage.setItem(NAV_KEY, JSON.stringify(tree)) } catch { /* quota */ }
  }, [tree, armed])

  const navigate = useCallback((fn) => {
    touched.current = true
    setTree(fn)
  }, [])

  // push(params) → deeper in the current tab. push(panel, params) → switch
  // tabs and go deeper there.
  const push = useCallback((panelOrParams, maybeParams) => {
    const explicit = typeof panelOrParams === 'string'
    const panel = explicit ? panelOrParams : null
    const params = explicit ? (maybeParams || {}) : (panelOrParams || {})
    navigate((s) => {
      const target = panel && PANELS.includes(panel) ? panel : s.activePanel
      const stack = s.stacks[target]
      const next = { panel: target, params }
      // Re-pushing the route you're already on is a no-op, not a duplicate
      // level — otherwise back would need two presses to do anything.
      if (routeKey(stack[stack.length - 1]) === routeKey(next)) {
        return target === s.activePanel ? s : { ...s, activePanel: target }
      }
      return {
        activePanel: target,
        stacks: { ...s.stacks, [target]: [...stack, next] },
      }
    })
  }, [navigate])

  // Lateral move: swap the top route's params without adding depth. Used for
  // things like picking a different tinker chat inside a card, so back still
  // goes card → board instead of walking every chat you opened.
  const replace = useCallback((patch) => {
    navigate((s) => {
      const stack = s.stacks[s.activePanel]
      const top = stack[stack.length - 1]
      const params = mergeParams(top.params, patch)
      if (serializeParams(params) === serializeParams(top.params)) return s
      return {
        ...s,
        stacks: {
          ...s.stacks,
          [s.activePanel]: [...stack.slice(0, -1), { panel: top.panel, params }],
        },
      }
    })
  }, [navigate])

  const pop = useCallback(() => {
    navigate((s) => {
      const stack = s.stacks[s.activePanel]
      if (stack.length <= 1) return s
      return { ...s, stacks: { ...s.stacks, [s.activePanel]: stack.slice(0, -1) } }
    })
  }, [navigate])

  const popToRoot = useCallback(() => {
    navigate((s) => {
      const stack = s.stacks[s.activePanel]
      if (stack.length <= 1) return s
      return { ...s, stacks: { ...s.stacks, [s.activePanel]: [stack[0]] } }
    })
  }, [navigate])

  // Switch tabs, keeping that tab's stack exactly as it was. An optional
  // params patch is merged into whatever route that tab is currently showing
  // (e.g. opening a session row → chat tab with a resumeId).
  const switchTab = useCallback((panel, patch) => {
    if (!PANELS.includes(panel)) return
    navigate((s) => {
      if (!patch) {
        return s.activePanel === panel ? s : { ...s, activePanel: panel }
      }
      const stack = s.stacks[panel]
      const top = stack[stack.length - 1]
      const params = mergeParams(top.params, patch)
      return {
        activePanel: panel,
        stacks: {
          ...s.stacks,
          [panel]: [...stack.slice(0, -1), { panel, params }],
        },
      }
    })
  }, [navigate])

  const { activePanel, stacks } = tree
  const stack = stacks[activePanel] || [rootRoute(activePanel)]
  const route = stack[stack.length - 1]

  // The route a given tab is currently showing, whether or not it's active —
  // needed by panels that stay mounted while hidden (Chat, Voice).
  const topRouteOf = useCallback((panel) => {
    const st = stacks[panel]
    return st && st.length ? st[st.length - 1] : rootRoute(panel)
  }, [stacks])

  const value = useMemo(() => ({
    activePanel,
    route,
    params: route.params,
    routeKey: routeKey(route),
    depth: stack.length,
    canGoBack: stack.length > 1,
    topRouteOf,
    push, replace, pop, popToRoot, switchTab,
  }), [activePanel, route, stack.length, topRouteOf, push, replace, pop, popToRoot, switchTab])

  // Global back gestures: ⌘/Ctrl+[ , Alt+←, and the mouse's back button.
  useEffect(() => {
    const onKey = (e) => {
      const inField = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target?.tagName || '')
        || e.target?.isContentEditable
      const chord = ((e.metaKey || e.ctrlKey) && e.key === '[') || (e.altKey && e.key === 'ArrowLeft')
      if (!chord || inField) return
      e.preventDefault()
      pop()
    }
    const onMouse = (e) => { if (e.button === 3) { e.preventDefault(); pop() } }
    window.addEventListener('keydown', onKey)
    window.addEventListener('mouseup', onMouse)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('mouseup', onMouse)
    }
  }, [pop])

  return <NavContext.Provider value={value}>{children}</NavContext.Provider>
}
