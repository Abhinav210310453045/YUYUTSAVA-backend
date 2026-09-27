import React, { useEffect, useRef, useState } from 'react'
import { getTaskLogs } from '../../api/client'
import { alpha } from '../../lib/color'
import './TaskDetail.css'

// Full-height drawer that shows how the deepagent drives one background
// sub-agent: its instruction, every tool call (name + args), each result/output,
// and the final summary — as an animated vertical timeline that live-updates
// while the task is still running. All data comes from GET /tasks/{id}/logs
// (already exposes tool_name / tool_args / status); no backend change needed.

const STATUS_STYLES = {
  running: { color: 'var(--neon-cyan, var(--text-cyan))', label: 'running' },
  awaiting_user: { color: 'var(--neon-amber, var(--neon-amber))', label: 'awaiting user' },
  success: { color: 'var(--neon-green, var(--neon-green))', label: 'done' },
  failed: { color: 'var(--neon-red, var(--neon-red))', label: 'failed' },
}

const NODE_COLORS = {
  user: 'var(--neon-purple, #a78bfa)',
  assistant: 'var(--neon-green, var(--neon-green))',
  tool_call: 'var(--neon-amber, var(--neon-amber))',
  tool_result: 'var(--neon-cyan, var(--text-cyan))',
}

function fmtElapsed(seconds) {
  if (seconds < 60) return `${Math.floor(seconds)}s`
  if (seconds < 3600)
    return `${Math.floor(seconds / 60)}m${String(Math.floor(seconds % 60)).padStart(2, '0')}s`
  return `${Math.floor(seconds / 3600)}h${String(Math.floor((seconds % 3600) / 60)).padStart(2, '0')}m`
}

function prettyArgs(raw) {
  if (raw == null || raw === '') return ''
  if (typeof raw === 'object') {
    try {
      return JSON.stringify(raw, null, 2)
    } catch {
      return String(raw)
    }
  }
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return String(raw)
  }
}

function TimelineItem({ msg, isLast }) {
  const [hover, setHover] = useState(false)
  const isErr = msg.status === 'error'
  const nodeColor = isErr
    ? 'var(--neon-red, var(--neon-red))'
    : NODE_COLORS[msg.role] || 'var(--text-muted)'

  let label = msg.role
  let body = msg.text || ''
  let mono = false
  if (msg.role === 'tool_call') {
    label = `→ ${msg.tool_name || 'tool'}`
    body = prettyArgs(msg.tool_args)
    mono = true
  } else if (msg.role === 'tool_result') {
    label = `← ${msg.tool_name || 'result'}`
    mono = true
  } else if (msg.role === 'user') {
    label = 'instruction'
  } else if (msg.role === 'assistant') {
    label = 'agent'
  }

  return (
    <div className="task-step">
      {/* left rail: node dot + connecting line */}
      <div className="task-step-rail">
        <span
          className="task-step-dot"
          style={{ background: nodeColor, boxShadow: `0 0 8px ${nodeColor}` }}
        />
        {!isLast && <span className="task-step-line" />}
      </div>

      {/* card */}
      <div
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        className="task-step-card"
        style={{
          border: `1px solid ${hover ? nodeColor : 'rgba(255,255,255,0.08)'}`,
          transform: hover ? 'translateY(-2px) scale(1.01)' : 'none',
          boxShadow: hover
            ? `0 6px 20px rgba(0,0,0,0.35), 0 0 12px ${alpha(nodeColor, 13)}`
            : 'none',
        }}
      >
        <div
          className="task-step-label"
          style={{
            color: isErr ? 'var(--neon-red, var(--neon-red))' : nodeColor,
            marginBottom: body ? 4 : 0,
          }}
        >
          {label}
        </div>
        {body && (
          <div
            className={`selectable task-step-body${mono ? ' task-step-body--mono' : ''}${
              isErr ? ' task-step-body--error' : ''
            }`}
          >
            {body}
          </div>
        )}
      </div>
    </div>
  )
}

export default function TaskDetail({ task, onClose }) {
  const [logs, setLogs] = useState(null)
  const [err, setErr] = useState(null)
  const [loading, setLoading] = useState(true)
  const [, force] = useState(0)
  const scrollRef = useRef(null)

  const isLive = task.status === 'running' || task.status === 'awaiting_user'
  const style = STATUS_STYLES[task.status] || { color: 'var(--text-secondary)', label: task.status }

  // Fetch immediately, then poll while the task is live so the timeline grows
  // in real time. Stops polling once the task reaches a terminal status.
  useEffect(() => {
    let cancelled = false
    let timer = null
    const pull = async () => {
      try {
        const res = await getTaskLogs(task.task_id)
        if (!cancelled) {
          setLogs(res.messages || [])
          setErr(null)
        }
      } catch (e) {
        if (!cancelled) setErr(e.message || String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    pull()
    if (isLive) timer = setInterval(pull, 1500)
    return () => {
      cancelled = true
      if (timer) clearInterval(timer)
    }
  }, [task.task_id, isLive])

  // Tick the elapsed clock while live.
  useEffect(() => {
    if (!isLive) return
    const t = setInterval(() => force((x) => x + 1), 1000)
    return () => clearInterval(t)
  }, [isLive])

  const startedAt = task.ts || task.last_update_at
  const elapsed = startedAt ? Math.max(0, Date.now() / 1000 - startedAt) : 0
  const instruction = (task.instruction_preview || task.text || '').toString()

  return (
    <div onClick={onClose} className="task-detail">
      <div onClick={(e) => e.stopPropagation()} className="task-detail-drawer">
        {/* header */}
        <div className="task-detail-head">
          <span
            className={`task-detail-status-dot${isLive ? ' task-detail-status-dot--live' : ''}`}
            style={{ background: style.color, boxShadow: `0 0 8px ${style.color}` }}
          />
          <div className="task-detail-titles">
            <div className="task-detail-row">
              <span className="task-detail-agent">{task.agent_name || '(unknown agent)'}</span>
              <span className="selectable task-detail-dim" title={task.task_id}>
                task={String(task.task_id || '').slice(0, 8)}
              </span>
            </div>
            <div className="task-detail-row task-detail-row--sub">
              <span className="task-detail-status" style={{ color: style.color }}>
                {style.label}
              </span>
              <span className="task-detail-dim">{fmtElapsed(elapsed)}</span>
            </div>
          </div>
          <button onClick={onClose} title="close" className="task-detail-close">
            ×
          </button>
        </div>

        {/* timeline */}
        <div ref={scrollRef} className="task-detail-timeline">
          {instruction && (
            <div className="task-detail-instruction">
              <div className="task-detail-kicker task-detail-kicker--task">Task</div>
              <div className="selectable task-detail-text">{instruction}</div>
            </div>
          )}

          {loading && logs === null && (
            <div className="task-detail-note">
              loading timeline<span className="task-detail-blink">…</span>
            </div>
          )}
          {err && <div className="task-detail-error">error: {err}</div>}

          {logs && logs.length === 0 && !loading && (
            <div className="task-detail-note">(no steps recorded yet)</div>
          )}

          {logs &&
            logs.map((m, i) => (
              <TimelineItem key={i} msg={m} isLast={!isLive && i === logs.length - 1} />
            ))}

          {isLive && (
            <div className="task-step">
              <div className="task-detail-live-rail">
                <span className="task-detail-live-dot" />
              </div>
              <div className="task-detail-live-text">
                {task.status === 'awaiting_user' ? 'waiting for your input…' : 'working…'}
              </div>
            </div>
          )}

          {!isLive && task.summary && (
            <div
              className="task-detail-result"
              style={{ border: `1px solid ${alpha(style.color, 27)}` }}
            >
              <div className="task-detail-kicker" style={{ color: style.color }}>
                Result
              </div>
              <div className="selectable task-detail-text">{task.summary}</div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
