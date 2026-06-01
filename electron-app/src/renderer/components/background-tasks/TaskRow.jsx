import React, { useEffect, useState } from 'react'

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

export default function TaskRow({ task }) {
  // Tick once per second so elapsed time updates for running tasks.
  const [, force] = useState(0)
  useEffect(() => {
    if (task.status === 'success' || task.status === 'failed') return
    const t = setInterval(() => force(x => x + 1), 1000)
    return () => clearInterval(t)
  }, [task.status])

  const startedAt = task.ts || task.last_update_at
  const now = Date.now() / 1000
  const elapsed = startedAt ? Math.max(0, now - startedAt) : 0
  const style = STATUS_STYLES[task.status] || { color: 'var(--text-secondary)', label: task.status }

  const summary = (task.summary || task.instruction_preview || task.text || '').toString()

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: '1fr auto',
      gap: 8,
      padding: '6px 8px',
      borderRadius: 4,
      background: 'rgba(255,255,255,0.02)',
      border: '1px solid rgba(255,255,255,0.06)',
      fontFamily: 'var(--font-mono)',
      fontSize: 11,
      lineHeight: 1.45,
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
  )
}
