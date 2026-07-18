import React, { useEffect, useMemo, useRef, useState } from 'react'
import TaskRow from '../background-tasks/TaskRow'
import TaskDetail from '../background-tasks/TaskDetail'

const KIND_STYLE = {
  log:            { color: '#5eead4' },
  token:          { color: 'var(--neon-green)' },
  tool_call:      { color: 'var(--neon-amber)', prefix: '→ ' },
  tool_result:    { color: 'var(--text-secondary)', prefix: '← ' },
  timeline:       { color: 'var(--text-primary)', borderLeft: '2px solid var(--neon-purple)', paddingLeft: 6 },
  http_log:       { color: 'var(--neon-amber)', prefix: 'HTTP ' },
  bg_task:        { color: 'var(--neon-cyan)' },
  system_metrics: { color: 'var(--text-dim)', prefix: '◴ ' },
  default:        { color: 'var(--text-muted)' },
}

function EventRow({ line, isLast }) {
  const [hover, setHover] = useState(false)
  const [copied, setCopied] = useState(false)
  const style = KIND_STYLE[line.kind] || KIND_STYLE.default
  const prefix = style.prefix || ''
  const copy = (e) => {
    e.stopPropagation()
    try {
      const raw = line.raw ?? { kind: line.kind, text: line.text, ts: line.ts }
      navigator.clipboard.writeText(JSON.stringify(raw, null, 2))
      setCopied(true)
      setTimeout(() => setCopied(false), 1000)
    } catch {}
  }
  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        fontFamily: 'var(--font-mono)',
        fontSize: 10,
        lineHeight: 1.5,
        padding: '1px 12px',
        display: 'flex',
        gap: 8,
        alignItems: 'flex-start',
        ...style,
        animation: isLast ? 'fade-in 0.2s ease' : undefined,
        position: 'relative',
        zIndex: 1,
      }}
    >
      <span style={{ color: 'var(--text-dim)', flexShrink: 0, fontSize: 9 }}>
        {fmtTime(line.ts || Date.now() / 1000)}
      </span>
      <span className="selectable" style={{ wordBreak: 'break-all', flex: 1 }}>
        {prefix}{line.text}
      </span>
      <button
        onClick={copy}
        title="Copy raw event JSON"
        style={{
          flexShrink: 0,
          fontFamily: 'var(--font-mono)',
          fontSize: 9,
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border-subtle)',
          color: copied ? 'var(--neon-green)' : 'var(--text-muted)',
          borderRadius: 3,
          padding: '0 4px',
          cursor: 'pointer',
          lineHeight: 1.4,
          opacity: hover || copied ? 1 : 0,
          transition: 'opacity 0.12s',
        }}
      >
        {copied ? '✓' : '⧉'}
      </button>
    </div>
  )
}

function fmtTime(ts) {
  const d = new Date(ts * 1000)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

const EMPTY_MAP = new Map()

const TABS = [
  { id: 'events', label: 'Events', emptyText: '> awaiting events...' },
  { id: 'logs',   label: 'Logs',   emptyText: '> awaiting logs...' },
  { id: 'tasks',  label: 'Tasks',  emptyText: '> no background tasks' },
]

export default function ActivityLog({ events = [], logs = [], bgTasks = EMPTY_MAP, width }) {
  const [tab, setTab] = useState('events')
  // Task whose full timeline drawer is open (null = closed). Kept fresh from the
  // live bgTasks map below so status/summary update while the drawer is open.
  const [detailId, setDetailId] = useState(null)
  // Tasks render as rows, not log lines — keep `lines` empty for that tab so the
  // auto-scroll effect below is a no-op there.
  const lines = tab === 'logs' ? logs : tab === 'events' ? events : []
  const bottomRef = useRef(null)
  const containerRef = useRef(null)
  const [autoScroll, setAutoScroll] = useState(true)
  const isTasks = tab === 'tasks'

  // Background-task split — mirrors the retired BackgroundTasksPanel: active
  // (running/awaiting) first, newest first; then completed/failed, newest first.
  const allTasks = useMemo(() => Array.from(bgTasks?.values() || []), [bgTasks])
  const activeTasks = useMemo(
    () => allTasks.filter(t => t.status === 'running' || t.status === 'awaiting_user')
                  .sort((a, b) => (b.last_update_at || 0) - (a.last_update_at || 0)),
    [allTasks],
  )
  const doneTasks = useMemo(
    () => allTasks.filter(t => t.status === 'success' || t.status === 'failed')
                  .sort((a, b) => (b.last_update_at || 0) - (a.last_update_at || 0)),
    [allTasks],
  )

  useEffect(() => {
    const el = containerRef.current
    if (!isTasks && autoScroll && el) {
      // Instant jump to bottom — smooth scrolling fights the rapid HTTP-log
      // stream and would never settle while lines keep arriving.
      el.scrollTop = el.scrollHeight
    }
  }, [lines, autoScroll, isTasks])

  // Reset scroll when switching tabs.
  useEffect(() => { setAutoScroll(true) }, [tab])

  const handleScroll = () => {
    const el = containerRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    setAutoScroll(atBottom)
  }

  const emptyText = TABS.find((t) => t.id === tab)?.emptyText || '> awaiting...'
  // Resolve the open drawer's task from the live map so it keeps updating; if the
  // task disappears, close the drawer.
  const detailTask = detailId ? bgTasks?.get?.(detailId) || null : null
  useEffect(() => { if (detailId && !detailTask) setDetailId(null) }, [detailId, detailTask])

  return (
    <div style={{
      width: width ?? 'var(--activity-w)',
      borderLeft: '1px solid var(--border-subtle)',
      background: 'var(--bg-panel)',
      display: 'flex',
      flexDirection: 'column',
      flex: 1,
      minHeight: 0,
      height: '100%',
      position: 'relative',
    }}>
      <div style={{
        display: 'flex',
        borderBottom: '1px solid var(--border-subtle)',
        background: 'var(--bg-bar)',
        flexShrink: 0,
      }}>
        {TABS.map((t) => {
          const count = t.id === 'logs' ? logs.length : t.id === 'tasks' ? (bgTasks?.size ?? 0) : events.length
          const active = t.id === tab
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              style={{
                flex: 1,
                padding: '8px 12px',
                fontFamily: 'var(--font-mono)',
                fontSize: 10,
                letterSpacing: '0.12em',
                textTransform: 'uppercase',
                background: active ? 'var(--bg-elevated)' : 'transparent',
                color: active ? 'var(--neon-green)' : 'var(--text-secondary)',
                border: 'none',
                borderBottom: active ? '1px solid var(--neon-green)' : '1px solid transparent',
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 6,
                transition: 'color 0.2s, background 0.2s',
              }}
            >
              <span>{t.label}</span>
              <span style={{
                fontSize: 9,
                color: active ? 'var(--neon-green)' : 'var(--text-dim)',
                opacity: 0.7,
              }}>
                {count > 999 ? '999+' : count}
              </span>
            </button>
          )
        })}
      </div>

      <div
        ref={containerRef}
        onScroll={handleScroll}
        style={{
          flex: 1,
          minHeight: 0,
          overflowY: 'auto',
          padding: '8px 0',
          position: 'relative',
        }}
      >
        {/* Subtle scan-line overlay */}
        <div style={{
          position: 'absolute',
          inset: 0,
          pointerEvents: 'none',
          backgroundImage: 'repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px)',
          zIndex: 0,
        }} />

        {isTasks ? (
          allTasks.length === 0 ? (
            <div style={{ padding: '20px 12px', color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', fontSize: 11 }}>
              {emptyText}
              <span style={{ animation: 'blink 1s step-end infinite' }}>_</span>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, padding: '4px 10px', position: 'relative', zIndex: 1 }}>
              {activeTasks.map(t => <TaskRow key={t.task_id} task={t} onOpen={() => setDetailId(t.task_id)} />)}
              {doneTasks.length > 0 && activeTasks.length > 0 && (
                <div style={{ borderTop: '1px solid rgba(255,255,255,0.06)', margin: '4px 0 0', paddingTop: 4 }} />
              )}
              {doneTasks.map(t => <TaskRow key={t.task_id} task={t} onOpen={() => setDetailId(t.task_id)} />)}
            </div>
          )
        ) : (
          <>
            {lines.length === 0 && (
              <div style={{ padding: '20px 12px', color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', fontSize: 11 }}>
                {emptyText}
                <span style={{ animation: 'blink 1s step-end infinite' }}>_</span>
              </div>
            )}

            {lines.map((line, i) => (
              <EventRow key={i} line={line} isLast={i === lines.length - 1} />
            ))}
            <div ref={bottomRef} />
          </>
        )}
      </div>

      {!isTasks && !autoScroll && (
        <button
          onClick={() => { setAutoScroll(true); bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }}
          style={{
            position: 'absolute',
            bottom: 12,
            right: 16,
            fontSize: 10,
            fontFamily: 'var(--font-mono)',
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border-neon)',
            color: 'var(--neon-green)',
            borderRadius: 4,
            padding: '3px 8px',
            cursor: 'pointer',
          }}
        >
          ↓ latest
        </button>
      )}

      {detailTask && (
        <TaskDetail task={detailTask} onClose={() => setDetailId(null)} />
      )}
    </div>
  )
}
