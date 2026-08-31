import React, { useEffect, useRef, useState } from 'react'
import { getTaskLogs } from '../../api/client'

// Full-height drawer that shows how the deepagent drives one background
// sub-agent: its instruction, every tool call (name + args), each result/output,
// and the final summary — as an animated vertical timeline that live-updates
// while the task is still running. All data comes from GET /tasks/{id}/logs
// (already exposes tool_name / tool_args / status); no backend change needed.

const STATUS_STYLES = {
  running:       { color: 'var(--neon-cyan, var(--text-cyan))', label: 'running' },
  awaiting_user: { color: 'var(--neon-amber, var(--neon-amber))', label: 'awaiting user' },
  success:       { color: 'var(--neon-green, var(--neon-green))', label: 'done' },
  failed:        { color: 'var(--neon-red, var(--neon-red))',   label: 'failed' },
}

const NODE_COLORS = {
  user:        'var(--neon-purple, #a78bfa)',
  assistant:   'var(--neon-green, var(--neon-green))',
  tool_call:   'var(--neon-amber, var(--neon-amber))',
  tool_result: 'var(--neon-cyan, var(--text-cyan))',
}

function fmtElapsed(seconds) {
  if (seconds < 60) return `${Math.floor(seconds)}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m${String(Math.floor(seconds % 60)).padStart(2, '0')}s`
  return `${Math.floor(seconds / 3600)}h${String(Math.floor((seconds % 3600) / 60)).padStart(2, '0')}m`
}

function prettyArgs(raw) {
  if (raw == null || raw === '') return ''
  if (typeof raw === 'object') {
    try { return JSON.stringify(raw, null, 2) } catch { return String(raw) }
  }
  try { return JSON.stringify(JSON.parse(raw), null, 2) } catch { return String(raw) }
}

function TimelineItem({ msg, isLast }) {
  const [hover, setHover] = useState(false)
  const isErr = msg.status === 'error'
  const nodeColor = isErr ? 'var(--neon-red, var(--neon-red))' : (NODE_COLORS[msg.role] || 'var(--text-muted)')

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
    <div style={{ display: 'grid', gridTemplateColumns: '20px 1fr', gap: 10 }}>
      {/* left rail: node dot + connecting line */}
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
        <span style={{
          width: 10, height: 10, borderRadius: '50%',
          background: nodeColor, marginTop: 12, flexShrink: 0,
          boxShadow: `0 0 8px ${nodeColor}`,
        }} />
        {!isLast && <span style={{ flex: 1, width: 2, background: 'rgba(255,255,255,0.08)', marginTop: 4 }} />}
      </div>

      {/* card */}
      <div
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        style={{
          marginBottom: 10,
          borderRadius: 8,
          background: 'var(--bg-card, rgba(255,255,255,0.03))',
          border: `1px solid ${hover ? nodeColor : 'rgba(255,255,255,0.08)'}`,
          padding: '8px 12px',
          animation: 'card-enter 0.25s ease',
          transition: 'transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease',
          transform: hover ? 'translateY(-2px) scale(1.01)' : 'none',
          boxShadow: hover ? `0 6px 20px rgba(0,0,0,0.35), 0 0 12px ${nodeColor}22` : 'none',
        }}
      >
        <div style={{
          color: isErr ? 'var(--neon-red, var(--neon-red))' : nodeColor,
          fontFamily: 'var(--font-mono)', fontSize: 11, fontWeight: 700,
          marginBottom: body ? 4 : 0, letterSpacing: '0.02em',
        }}>
          {label}
        </div>
        {body && (
          <div
            className="selectable"
            style={{
              color: isErr ? 'var(--neon-red, var(--neon-red))' : 'var(--text-secondary)',
              fontFamily: mono ? 'var(--font-mono)' : 'var(--font-ui)',
              fontSize: mono ? 11 : 12.5, lineHeight: 1.5,
              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
              maxHeight: 260, overflow: 'auto',
            }}
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
        if (!cancelled) { setLogs(res.messages || []); setErr(null) }
      } catch (e) {
        if (!cancelled) setErr(e.message || String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    pull()
    if (isLive) timer = setInterval(pull, 1500)
    return () => { cancelled = true; if (timer) clearInterval(timer) }
  }, [task.task_id, isLive])

  // Tick the elapsed clock while live.
  useEffect(() => {
    if (!isLive) return
    const t = setInterval(() => force(x => x + 1), 1000)
    return () => clearInterval(t)
  }, [isLive])

  const startedAt = task.ts || task.last_update_at
  const elapsed = startedAt ? Math.max(0, Date.now() / 1000 - startedAt) : 0
  const instruction = (task.instruction_preview || task.text || '').toString()

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 50,
        background: 'rgba(0,0,0,0.5)', backdropFilter: 'blur(2px)',
        display: 'flex', justifyContent: 'flex-end',
        animation: 'fade-in 0.15s ease',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 'min(720px, 94vw)', height: '100%',
          background: 'var(--bg-panel)', borderLeft: '1px solid var(--border-neon, rgba(var(--accent-rgb),0.25))',
          display: 'flex', flexDirection: 'column',
          boxShadow: '-24px 0 60px rgba(0,0,0,0.5)',
          animation: 'drawer-slide-in 0.22s ease',
        }}
      >
        {/* header */}
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '14px 18px', borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-bar)',
          flexShrink: 0,
        }}>
          <span style={{
            width: 9, height: 9, borderRadius: '50%', background: style.color,
            boxShadow: `0 0 8px ${style.color}`,
            animation: isLive ? 'task-node-pulse 1.4s ease-out infinite' : 'none',
          }} />
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ color: 'var(--text-primary)', fontWeight: 700, fontFamily: 'var(--font-mono)', fontSize: 13 }}>
                {task.agent_name || '(unknown agent)'}
              </span>
              <span className="selectable" title={task.task_id} style={{ color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', fontSize: 11 }}>
                task={String(task.task_id || '').slice(0, 8)}
              </span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 2 }}>
              <span style={{ color: style.color, fontFamily: 'var(--font-mono)', fontSize: 11, fontWeight: 'var(--fw-semibold)' }}>
                {style.label}
              </span>
              <span style={{ color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', fontSize: 11 }}>
                {fmtElapsed(elapsed)}
              </span>
            </div>
          </div>
          <button
            onClick={onClose}
            title="close"
            style={{
              background: 'none', border: 'none', color: 'var(--text-muted)',
              cursor: 'pointer', fontSize: 20, lineHeight: 1, padding: '2px 6px',
            }}
          >×</button>
        </div>

        {/* timeline */}
        <div ref={scrollRef} style={{ flex: 1, overflowY: 'auto', padding: '16px 18px' }}>
          {instruction && (
            <div style={{
              marginBottom: 14, padding: '10px 12px', borderRadius: 8,
              background: 'rgba(167,139,250,0.06)', border: '1px solid rgba(167,139,250,0.25)',
            }}>
              <div style={{ color: 'var(--neon-purple, #a78bfa)', fontFamily: 'var(--font-mono)', fontSize: 10, fontWeight: 700, letterSpacing: '0.12em', textTransform: 'uppercase', marginBottom: 4 }}>
                Task
              </div>
              <div className="selectable" style={{ color: 'var(--text-secondary)', fontSize: 12.5, lineHeight: 1.5 }}>
                {instruction}
              </div>
            </div>
          )}

          {loading && logs === null && (
            <div style={{ color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
              loading timeline<span style={{ animation: 'blink 1s step-end infinite' }}>…</span>
            </div>
          )}
          {err && <div style={{ color: 'var(--neon-red, var(--neon-red))', fontFamily: 'var(--font-mono)', fontSize: 12 }}>error: {err}</div>}

          {logs && logs.length === 0 && !loading && (
            <div style={{ color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
              (no steps recorded yet)
            </div>
          )}

          {logs && logs.map((m, i) => (
            <TimelineItem key={i} msg={m} isLast={!isLive && i === logs.length - 1} />
          ))}

          {isLive && (
            <div style={{ display: 'grid', gridTemplateColumns: '20px 1fr', gap: 10 }}>
              <div style={{ display: 'flex', justifyContent: 'center' }}>
                <span style={{
                  width: 10, height: 10, borderRadius: '50%', marginTop: 12,
                  background: 'var(--neon-cyan, var(--text-cyan))',
                  animation: 'task-node-pulse 1.4s ease-out infinite',
                }} />
              </div>
              <div style={{ color: 'var(--neon-cyan, var(--text-cyan))', fontFamily: 'var(--font-mono)', fontSize: 11, padding: '10px 0' }}>
                {task.status === 'awaiting_user' ? 'waiting for your input…' : 'working…'}
              </div>
            </div>
          )}

          {!isLive && task.summary && (
            <div style={{
              marginTop: 8, padding: '10px 12px', borderRadius: 8,
              background: 'rgba(var(--accent-rgb),0.05)', border: `1px solid ${style.color}44`,
            }}>
              <div style={{ color: style.color, fontFamily: 'var(--font-mono)', fontSize: 10, fontWeight: 700, letterSpacing: '0.12em', textTransform: 'uppercase', marginBottom: 4 }}>
                Result
              </div>
              <div className="selectable" style={{ color: 'var(--text-secondary)', fontSize: 12.5, lineHeight: 1.5 }}>
                {task.summary}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
