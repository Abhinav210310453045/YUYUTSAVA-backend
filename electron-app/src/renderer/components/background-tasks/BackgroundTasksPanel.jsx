import React, { useMemo, useState } from 'react'
import { useSSE } from '../../hooks/useSSE'
import TaskRow from './TaskRow'

/**
 * Background subagent tasks panel.
 *
 * Subscribes to ``useSSE().bgTasks`` (a Map keyed by task_id). Renders rows
 * sorted by status — running/awaiting_user first (newest first), then
 * completed/failed (most recent first). Empty state collapses the panel
 * footer to a single status line so unused screen real estate stays small.
 */
export default function BackgroundTasksPanel() {
  const { bgTasks } = useSSE()
  const [collapsed, setCollapsed] = useState(false)

  const all = useMemo(() => Array.from(bgTasks?.values() || []), [bgTasks])
  const active = useMemo(
    () => all.filter(t => t.status === 'running' || t.status === 'awaiting_user')
              .sort((a, b) => (b.last_update_at || 0) - (a.last_update_at || 0)),
    [all],
  )
  const done = useMemo(
    () => all.filter(t => t.status === 'success' || t.status === 'failed')
              .sort((a, b) => (b.last_update_at || 0) - (a.last_update_at || 0)),
    [all],
  )

  if (all.length === 0) {
    // Nothing to show — render a one-line footer placeholder so the section
    // is discoverable when the first task arrives but doesn't take space.
    return (
      <div style={{
        padding: '6px 8px',
        fontFamily: 'var(--font-mono)',
        fontSize: 10,
        color: 'var(--text-dim)',
        textTransform: 'uppercase',
        letterSpacing: '0.08em',
      }}>
        background tasks — none
      </div>
    )
  }

  return (
    <div style={{
      padding: '8px 10px',
      display: 'flex',
      flexDirection: 'column',
      gap: 6,
      maxHeight: '40vh',
      overflowY: 'auto',
    }}>
      <div
        onClick={() => setCollapsed(c => !c)}
        style={{
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          color: 'var(--text-secondary)',
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
          userSelect: 'none',
        }}
      >
        <span>{collapsed ? '▶' : '▼'}</span>
        <span>background tasks</span>
        <span style={{ color: 'var(--text-dim)', textTransform: 'none', letterSpacing: 0 }}>
          {active.length} active{done.length ? ` · ${done.length} done` : ''}
        </span>
      </div>

      {!collapsed && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {active.map(t => <TaskRow key={t.task_id} task={t} />)}
          {done.length > 0 && active.length > 0 && (
            <div style={{
              borderTop: '1px solid rgba(255,255,255,0.06)',
              margin: '4px 0 0',
              paddingTop: 4,
            }} />
          )}
          {done.map(t => <TaskRow key={t.task_id} task={t} />)}
        </div>
      )}
    </div>
  )
}
