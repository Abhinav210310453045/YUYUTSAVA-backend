import React, { useEffect, useState } from 'react'
import { getTaskLogs } from '../../api/client'

function fmtElapsed(seconds) {
  if (seconds < 60) return `${Math.floor(seconds)}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m${String(Math.floor(seconds % 60)).padStart(2, '0')}s`
  return `${Math.floor(seconds / 3600)}h${String(Math.floor((seconds % 3600) / 60)).padStart(2, '0')}m`
}

const STATUS_STYLES = {
  running:        { color: 'var(--neon-cyan, #22d3ee)', label: 'running' },
  awaiting_user:  { color: 'var(--neon-amber, #fbbf24)', label: 'awaiting user' },
  success:        { color: 'var(--neon-green, #00ff88)', label: 'done' },
  failed:         { color: 'var(--neon-red, #ff3366)',   label: 'failed' },
}

export default function TaskRow({ task, onOpen }) {
  // Tick once per second so elapsed time updates for running tasks.
  const [, force] = useState(0)
  const [hover, setHover] = useState(false)
  useEffect(() => {
    if (task.status === 'success' || task.status === 'failed') return
    const t = setInterval(() => force(x => x + 1), 1000)
    return () => clearInterval(t)
  }, [task.status])

  // Lazy-loaded full transcript (fetched only when the row is expanded), so we
  // never push large per-task logs into the SSE reducer.
  const [open, setOpen] = useState(false)
  const [logs, setLogs] = useState(null)
  const [logsErr, setLogsErr] = useState(null)
  const [loading, setLoading] = useState(false)

  const loadLogs = async () => {
    setLoading(true)
    setLogsErr(null)
    try {
      const res = await getTaskLogs(task.task_id)
      setLogs(res.messages || [])
    } catch (e) {
      setLogsErr(e.message || String(e))
    } finally {
      setLoading(false)
    }
  }

  const toggleLogs = () => {
    const next = !open
    setOpen(next)
    if (next && logs === null && !loading) loadLogs()
  }

  const startedAt = task.ts || task.last_update_at
  const now = Date.now() / 1000
  const elapsed = startedAt ? Math.max(0, now - startedAt) : 0
  const style = STATUS_STYLES[task.status] || { color: 'var(--text-secondary)', label: task.status }

  const summary = (task.summary || task.instruction_preview || task.text || '').toString()

  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        borderRadius: 4,
        background: 'rgba(255,255,255,0.02)',
        border: `1px solid ${hover ? style.color : 'rgba(255,255,255,0.06)'}`,
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        lineHeight: 1.45,
        transition: 'transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease',
        transform: hover ? 'translateY(-2px) scale(1.01)' : 'none',
        boxShadow: hover ? `0 6px 18px rgba(0,0,0,0.35), 0 0 10px ${style.color}22` : 'none',
      }}>
      <div
        onClick={() => onOpen && onOpen(task)}
        title="open task detail"
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr auto',
          gap: 8,
          padding: '6px 8px',
          cursor: onOpen ? 'pointer' : 'default',
        }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>
              {task.agent_name || '(unknown)'}
            </span>
            <span title={task.task_id} style={{ color: 'var(--text-dim)' }}>
              task={String(task.task_id || '').slice(0, 8)}
            </span>
          </div>
          {summary && (
            <div style={{
              color: 'var(--text-secondary)',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              marginTop: 2,
            }}>
              {summary}
            </div>
          )}
          <button
            onClick={(e) => { e.stopPropagation(); toggleLogs() }}
            style={{
              marginTop: 4,
              background: 'none',
              border: 'none',
              padding: 0,
              cursor: 'pointer',
              color: 'var(--neon-cyan, #22d3ee)',
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
            }}
          >
            {open ? '▾ hide quick logs' : '▸ quick logs'}
          </button>
          {onOpen && (
            <span style={{ color: 'var(--text-dim)', fontSize: 10, marginLeft: 10 }}>
              ⤢ open detail
            </span>
          )}
        </div>
        <div style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
          <span style={{ color: style.color, fontWeight: 600 }}>
            {style.label}
          </span>
          <span style={{ color: 'var(--text-dim)', marginLeft: 6 }}>
            {fmtElapsed(elapsed)}
          </span>
        </div>
      </div>
      {open && (
        <div style={{
          borderTop: '1px solid rgba(255,255,255,0.06)',
          padding: '6px 8px',
          maxHeight: 240,
          overflowY: 'auto',
        }}>
          {loading && <div style={{ color: 'var(--text-dim)' }}>loading logs…</div>}
          {logsErr && <div style={{ color: 'var(--neon-red, #ff3366)' }}>error: {logsErr}</div>}
          {!loading && !logsErr && logs && logs.length === 0 && (
            <div style={{ color: 'var(--text-dim)' }}>(no log messages yet)</div>
          )}
          {!loading && !logsErr && logs && logs.map((m, i) => (
            <TaskLogLine key={i} msg={m} />
          ))}
          {!loading && !logsErr && logs && (
            <button
              onClick={loadLogs}
              style={{
                marginTop: 4, background: 'none', border: 'none', padding: 0,
                cursor: 'pointer', color: 'var(--text-dim)',
                fontFamily: 'var(--font-mono)', fontSize: 10,
              }}
            >
              ⟳ refresh
            </button>
          )}
        </div>
      )}
    </div>
  )
}

const ROLE_COLORS = {
  assistant:   'var(--text-primary)',
  user:        'var(--neon-green, #00ff88)',
  tool_call:   'var(--neon-cyan, #22d3ee)',
  tool_result: 'var(--text-secondary)',
}

function TaskLogLine({ msg }) {
  const color = ROLE_COLORS[msg.role] || 'var(--text-secondary)'
  const isErr = msg.status === 'error'
  let label = msg.role
  let body = msg.text
  if (msg.role === 'tool_call') {
    label = `→ ${msg.tool_name || 'tool'}`
    body = msg.tool_args || ''
  } else if (msg.role === 'tool_result') {
    label = `← ${msg.tool_name || 'result'}`
  }
  return (
    <div style={{ marginBottom: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
      <span style={{ color: isErr ? 'var(--neon-red, #ff3366)' : color, fontWeight: 600 }}>
        {label}:
      </span>{' '}
      <span style={{ color: isErr ? 'var(--neon-red, #ff3366)' : 'var(--text-secondary)' }}>
        {body}
      </span>
    </div>
  )
}
