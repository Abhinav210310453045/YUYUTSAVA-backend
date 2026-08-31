import { createContext, useContext, useReducer, useEffect, useRef } from 'react'
import { SSEClient } from '../api/sse'

const MAX_LINES = 2000
// Kinds routed to the "Logs" tab. Everything else goes to "Events".
const LOG_KINDS = new Set(['http_log'])
// Kinds handled by the Background Tasks panel — not routed to event/log lines.
const BG_TASK_KINDS = new Set([
  'async_task_started',
  'async_task_progress',
  'async_task_awaiting_user',
  'async_task_completed',
])
// Keep at most this many completed bg tasks in the panel before evicting.
const MAX_COMPLETED_BG_TASKS = 30
const LOGS_ENABLED_KEY = 'yuyutsava.logsInUI'

// Module-level mutable flag so the SSE onEvent callback (captured once in
// useEffect) can read the latest UI toggle state without re-subscribing.
const logsFlag = { enabled: readLogsEnabled() }

function readLogsEnabled() {
  try {
    const v = localStorage.getItem(LOGS_ENABLED_KEY)
    return v === null ? true : v === '1'
  } catch {
    return true
  }
}

export function setLogsEnabled(enabled) {
  logsFlag.enabled = enabled
  try { localStorage.setItem(LOGS_ENABLED_KEY, enabled ? '1' : '0') } catch {}
}

export function getLogsEnabled() {
  return logsFlag.enabled
}

// --- readable line formatters -------------------------------------------
// Events lines are human-readable; the raw JSON payload is never dumped into
// the line (it's available via each row's copy button instead).

function fmtArgs(args) {
  if (!args || typeof args !== 'object') return ''
  return Object.entries(args)
    .map(([k, v]) => {
      let s = typeof v === 'string' ? v : JSON.stringify(v)
      if (s && s.length > 60) s = s.slice(0, 60) + '…'
      return `${k}=${s}`
    })
    .join(', ')
}

function fmtMetrics(d) {
  const cpu = d?.cpu_pct != null ? `cpu ${Math.round(d.cpu_pct)}%` : null
  const mem = d?.mem_available_mb != null ? `mem ${Math.round(d.mem_available_mb)}MB` : null
  const disk = d?.disk_free_gb != null ? `disk ${Math.round(d.disk_free_gb)}GB` : null
  return [cpu, mem, disk].filter(Boolean).join(' · ') || 'resources'
}

// Owner-labeled background-task line so progress is attributable at a glance.
function fmtBgTask(kind, d) {
  const who = `${d?.agent_name || 'bg task'} · ${(d?.task_id || '').slice(0, 8)}`
  if (kind === 'async_task_started')
    return `[bg ${who}] started${d?.instruction_preview ? ': ' + d.instruction_preview : ''}`
  if (kind === 'async_task_progress') {
    const arrow = d?.kind_hint === 'tool_call' ? '→ ' : ''
    return `[bg ${who}] ${arrow}${d?.text || ''}`.trimEnd()
  }
  if (kind === 'async_task_awaiting_user')
    return `[bg ${who}] ⏸ awaiting approval: ${d?.title || ''}`.trimEnd()
  if (kind === 'async_task_completed')
    return `[bg ${who}] ${d?.ok ? '✓ done' : '✗ failed'}${d?.summary ? ': ' + d.summary : ''}`
  return `[bg ${who}] ${kind}`
}

const SSEContext = createContext(null)

function reducer(state, action) {
  switch (action.type) {
    case 'CONNECTED':
      return { ...state, connected: true }
    case 'DISCONNECTED':
      return { ...state, connected: false }
    case 'PROPOSAL': {
      const p = action.payload.proposal
      const proposals = new Map(state.proposals)
      proposals.set(p.proposal_id, p)
      return { ...state, proposals, pendingCount: proposals.size + state.asks.size }
    }
    case 'ASK': {
      const a = action.payload.ask
      const asks = new Map(state.asks)
      asks.set(a.ask_id, a)
      return { ...state, asks, pendingCount: state.proposals.size + asks.size }
    }
    case 'REMOVE_PROPOSAL': {
      const proposals = new Map(state.proposals)
      proposals.delete(action.id)
      return { ...state, proposals, pendingCount: proposals.size + state.asks.size }
    }
    case 'REMOVE_ASK': {
      const asks = new Map(state.asks)
      asks.delete(action.id)
      return { ...state, asks, pendingCount: state.proposals.size + asks.size }
    }
    case 'EVENT_LINE': {
      const eventLines = [...state.eventLines, action.line]
      return { ...state, eventLines: eventLines.length > MAX_LINES ? eventLines.slice(-MAX_LINES) : eventLines }
    }
    case 'LOG_LINE': {
      const logLines = [...state.logLines, action.line]
      return { ...state, logLines: logLines.length > MAX_LINES ? logLines.slice(-MAX_LINES) : logLines }
    }
    case 'BG_TASK': {
      // ``payload`` is the inner ``data`` from a ``async_task_*`` event.
      const kind = action.kind
      const data = action.payload
      const taskId = data?.task_id
      if (!taskId) return state
      const bgTasks = new Map(state.bgTasks)
      const prev = bgTasks.get(taskId) || { task_id: taskId, status: 'running', events: [] }
      let status = prev.status
      if (kind === 'async_task_started') status = 'running'
      else if (kind === 'async_task_awaiting_user') status = 'awaiting_user'
      else if (kind === 'async_task_completed') status = data?.ok ? 'success' : 'failed'
      // progress: status unchanged
      const next = {
        ...prev,
        ...data,
        status,
        last_kind: kind,
        last_update_at: data?.ts || (Date.now() / 1000),
      }
      bgTasks.set(taskId, next)
      // Trim completed/failed beyond the cap (FIFO by last_update_at).
      const finished = Array.from(bgTasks.values()).filter(
        t => t.status === 'success' || t.status === 'failed',
      )
      if (finished.length > MAX_COMPLETED_BG_TASKS) {
        finished.sort((a, b) => (a.last_update_at || 0) - (b.last_update_at || 0))
        const evict = finished.slice(0, finished.length - MAX_COMPLETED_BG_TASKS)
        for (const t of evict) bgTasks.delete(t.task_id)
      }
      return { ...state, bgTasks }
    }
    default:
      return state
  }
}

const initialState = {
  proposals: new Map(),
  asks: new Map(),
  eventLines: [],
  logLines: [],
  bgTasks: new Map(),     // task_id -> { task_id, agent_name, status, ... }
  connected: false,
  pendingCount: 0,
}

export function SSEProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, initialState)
  const clientRef = useRef(null)

  useEffect(() => {
    const client = new SSEClient({
      onConnected: () => dispatch({ type: 'CONNECTED' }),
      onDisconnected: () => dispatch({ type: 'DISCONNECTED' }),
      onProposal: (data) => dispatch({ type: 'PROPOSAL', payload: data }),
      onAsk: (data) => dispatch({ type: 'ASK', payload: data }),
      // Resolved elsewhere (CLI answer, expiry, watcher auto-reject): drop the card.
      onAskResolved: (data) => dispatch({ type: 'REMOVE_ASK', id: data.ask_id }),
      onProposalResolved: (data) => dispatch({ type: 'REMOVE_PROPOSAL', id: data.proposal_id }),
      // Wake word detected by the daemon → ask main to pop the voice overlay
      // (or route to the in-app Voice panel if the window is focused). Main owns
      // that decision; the renderer just forwards the signal + wake word.
      onWake: (data) => {
        // Two-stage wake: stage "open" fires instantly on detection → pop the
        // overlay; stage "command" carries the same-breath trailing command →
        // main relays it to the (already-open) overlay to seed the first turn
        // instead of re-popping. Main owns the overlay-vs-panel decision.
        try {
          window.electronAPI?.notifyVoiceWake?.({
            wakeWord: data?.wake_word || '',
            stage: data?.stage || 'open',
            command: data?.command || '',
          })
        } catch {}
      },
      onEvent: (data) => {
        const kind = data.kind || 'log'
        const d = data.data || {}
        const ts = d.ts || Date.now() / 1000
        // Background subagent events: update the Tasks panel AND surface an
        // owner-labeled line in the Events stream so progress is attributable.
        // Also fire a focus-aware OS notification on completion.
        if (BG_TASK_KINDS.has(kind)) {
          dispatch({ type: 'BG_TASK', kind, payload: d })
          if (kind === 'async_task_completed') {
            const ok = !!d.ok
            const agent = d.agent_name || 'background task'
            try {
              // Preload exposes the IPC as ``showNotification`` (preload.js:35
              // → ipcMain ``notify:show`` handler in main/notifications.js).
              window.electronAPI?.showNotification?.({
                title: ok ? `${agent} ✓ completed` : `${agent} ✗ failed`,
                body: (d.summary || '').slice(0, 200),
              })
            } catch {}
          }
          dispatch({
            type: 'EVENT_LINE',
            line: { kind: 'bg_task', text: fmtBgTask(kind, d), ts, raw: data },
          })
          return
        }
        // Token fragments are the model's streaming reply — rendered in the
        // chat view, not the activity log. Skip them so Events stays readable.
        if (kind === 'token') return
        const isLog = LOG_KINDS.has(kind)
        // Logs are gated by the Titlebar toggle; events always flow.
        if (isLog && !logsFlag.enabled) return
        let text = ''
        if (kind === 'tool_call') text = `${d.name || ''}(${fmtArgs(d.args)})`
        else if (kind === 'tool_result') text = `${d.name || ''}: ${String(d.preview ?? '').replace(/\s+/g, ' ').slice(0, 200)}`
        else if (kind === 'timeline') text = d.line || d.text || d.summary || ''
        else if (kind === 'system_metrics') text = fmtMetrics(d)
        else if (kind === 'http_log') text = `${d.method} ${d.path} → ${d.status} (${d.duration_ms}ms)`
        else text = d.text || d.message || kind

        dispatch({
          type: isLog ? 'LOG_LINE' : 'EVENT_LINE',
          line: { kind, text, ts, raw: data },
        })
      },
    })
    clientRef.current = client
    client.connect()
    return () => client.disconnect()
  }, [])

  // Update tray badge when pending count changes
  useEffect(() => {
    window.electronAPI?.setProposalCount(state.pendingCount)
  }, [state.pendingCount])

  const removeProposal = (id) => dispatch({ type: 'REMOVE_PROPOSAL', id })
  const removeAsk = (id) => dispatch({ type: 'REMOVE_ASK', id })

  return (
    <SSEContext.Provider value={{ ...state, removeProposal, removeAsk }}>
      {children}
    </SSEContext.Provider>
  )
}

export function useSSE() {
  return useContext(SSEContext)
}
