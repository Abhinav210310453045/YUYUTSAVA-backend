import React, { useState } from 'react'
import { deleteSession } from '../../api/client'

function humanBytes(n) {
  if (n < 1024) return `${n}B`
  let v = n
  for (const u of ['KB', 'MB', 'GB', 'TB']) {
    v /= 1024
    if (v < 1024) return `${v.toFixed(1)}${u}`
  }
  return `${v.toFixed(1)}PB`
}

function humanAge(unixSec) {
  const d = Math.max(0, Date.now() / 1000 - unixSec)
  if (d < 60) return `${Math.floor(d)}s ago`
  if (d < 3600) return `${Math.floor(d / 60)}m ago`
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`
  return `${Math.floor(d / 86400)}d ago`
}

// Shell-quote: wrap in single quotes; escape embedded single quotes.
function shellQuote(s) {
  return `'` + String(s).replace(/'/g, `'\\''`) + `'`
}

const STATUS_COLOR = {
  running: 'var(--neon-green)',
  idle: '#facc15',
  crashed: 'var(--neon-red)',
  done: 'var(--text-dim)',
}

export default function SessionRow({ session, onDeleted }) {
  const [copied, setCopied] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const shortId = session.id.length > 8 ? `${session.id.slice(0, 8)}…` : session.id
  const wsBase = session.workspace.split('/').filter(Boolean).pop() || session.workspace
  const dotColor = STATUS_COLOR[session.status] || 'var(--text-muted)'

  const resumeCmd =
    `uv run yuyutsava --verbose --workspace ${shellQuote(session.workspace)} ` +
    `--resume ${session.id} "<your next message>"`

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(resumeCmd)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard blocked — silently ignore */
    }
  }

  const onDelete = async () => {
    if (!confirm(`Delete session ${shortId}?\n\nThis also deletes the LangGraph checkpoint rows.`)) return
    setDeleting(true)
    try {
      await deleteSession(session.id)
      onDeleted?.(session.id)
    } catch (e) {
      alert(`Delete failed: ${e.message}`)
      setDeleting(false)
    }
  }

  return (
    <div style={{
      background: 'var(--bg-card, #11131c)',
      border: '1px solid var(--border-subtle, #1f2233)',
      borderRadius: 8,
      padding: '12px 14px',
      fontFamily: 'var(--font-mono)',
      fontSize: 12,
      color: 'var(--text-primary)',
      display: 'flex',
      flexDirection: 'column',
      gap: 8,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span
          aria-label={session.status}
          title={session.status}
          style={{
            width: 8, height: 8, borderRadius: '50%',
            background: dotColor,
            flexShrink: 0,
            boxShadow: session.status === 'running' ? `0 0 6px ${dotColor}` : 'none',
          }}
        />
        <span title={session.id} style={{ color: 'var(--neon-green)', fontWeight: 600 }}>
          {shortId}
        </span>
        <span style={{ color: 'var(--text-dim)' }}>·</span>
        <span title={session.workspace} style={{ color: 'var(--text-muted)' }}>
          {wsBase}
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ color: 'var(--text-dim)' }}>{humanAge(session.updated_at)}</span>
      </div>

      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', color: 'var(--text-muted)', fontSize: 11 }}>
        <span>msgs: {session.message_count}</span>
        <span>·</span>
        <span>mem: {session.memory_files_count}</span>
        <span>·</span>
        <span>{humanBytes(session.db_row_bytes)}</span>
      </div>

      {session.task_preview && (
        <div style={{
          color: 'var(--text-primary)',
          fontSize: 12,
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          opacity: 0.85,
        }}
          title={session.task_preview}
        >
          {session.task_preview}
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, marginTop: 2 }}>
        <button
          onClick={onCopy}
          style={{
            flex: 1,
            fontFamily: 'var(--font-mono)',
            fontSize: 11,
            padding: '6px 10px',
            background: copied ? 'rgba(0,255,136,0.18)' : 'rgba(0,255,136,0.06)',
            color: 'var(--neon-green)',
            border: '1px solid rgba(0,255,136,0.25)',
            borderRadius: 6,
            cursor: 'pointer',
            transition: 'background 0.15s',
          }}
        >
          {copied ? 'Copied!' : 'Copy resume'}
        </button>
        <button
          onClick={onDelete}
          disabled={deleting}
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 11,
            padding: '6px 10px',
            background: 'transparent',
            color: 'var(--neon-red)',
            border: '1px solid rgba(255,51,102,0.25)',
            borderRadius: 6,
            cursor: deleting ? 'default' : 'pointer',
            opacity: deleting ? 0.5 : 1,
          }}
        >
          {deleting ? '...' : 'Delete'}
        </button>
      </div>
    </div>
  )
}
