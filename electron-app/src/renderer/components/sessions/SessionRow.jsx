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

const CHAT_PREVIEW_MARKER = '(interactive chat)'

function CopyButton({ label, command, accent = 'var(--neon-green)' }) {
  const [copied, setCopied] = useState(false)
  const onCopy = async (e) => {
    e.stopPropagation()
    try {
      await navigator.clipboard.writeText(command)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard blocked — silently ignore */
    }
  }
  return (
    <button
      onClick={onCopy}
      title={command}
      style={{
        flex: 1,
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        padding: '6px 10px',
        background: copied ? 'rgba(0,255,136,0.18)' : 'rgba(0,255,136,0.06)',
        color: accent,
        border: `1px solid ${accent === 'var(--neon-green)' ? 'rgba(0,255,136,0.25)' : 'rgba(255,200,80,0.3)'}`,
        borderRadius: 6,
        cursor: 'pointer',
        transition: 'background 0.15s',
        whiteSpace: 'nowrap',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
      }}
    >
      {copied ? 'Copied!' : label}
    </button>
  )
}

export default function SessionRow({ session, onDeleted }) {
  const [deleting, setDeleting] = useState(false)
  const [idExpanded, setIdExpanded] = useState(false)

  const shortId = session.id.length > 8 ? `${session.id.slice(0, 8)}…` : session.id
  const wsBase = session.workspace.split('/').filter(Boolean).pop() || session.workspace
  const dotColor = STATUS_COLOR[session.status] || 'var(--text-muted)'
  const isChat = (session.task_preview || '').trim() === CHAT_PREVIEW_MARKER

  const cliResumeCmd =
    `uv run yuyutsava --verbose --workspace ${shellQuote(session.workspace)} ` +
    `--resume ${session.id} "<your next message>"`
  const chatResumeCmd =
    `uv run yuyutsava chat --verbose --workspace ${shellQuote(session.workspace)} ` +
    `--resume ${session.id}`

  const onDelete = async (e) => {
    e.stopPropagation()
    if (!confirm(`Delete session ${shortId}?\n\nThis also deletes the LangGraph checkpoint rows.`)) return
    setDeleting(true)
    try {
      await deleteSession(session.id)
      onDeleted?.(session.id)
    } catch (err) {
      alert(`Delete failed: ${err.message}`)
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
        <span
          onClick={() => setIdExpanded((v) => !v)}
          title={idExpanded ? 'click to collapse' : session.id}
          style={{
            color: 'var(--neon-green)',
            fontWeight: 600,
            cursor: 'pointer',
            display: 'inline-block',
            maxWidth: idExpanded ? '480px' : '90px',
            overflow: 'hidden',
            whiteSpace: 'nowrap',
            textOverflow: 'clip',
            transition: 'max-width 280ms cubic-bezier(0.4, 0, 0.2, 1)',
            userSelect: 'text',
          }}
        >
          {idExpanded ? session.id : shortId}
        </span>
        {isChat && (
          <span style={{
            fontSize: 9,
            padding: '1px 6px',
            borderRadius: 8,
            background: 'rgba(120, 160, 255, 0.12)',
            color: '#9bb8ff',
            border: '1px solid rgba(120, 160, 255, 0.25)',
            textTransform: 'uppercase',
            letterSpacing: '0.05em',
          }}>
            chat
          </span>
        )}
        <span style={{ color: 'var(--text-dim)' }}>·</span>
        <span title={session.workspace} style={{ color: 'var(--text-muted)' }}>
          {wsBase}
        </span>
        <span style={{ flex: 1 }} />
        <span style={{ color: 'var(--text-dim)' }}>{humanAge(session.updated_at)}</span>
      </div>

      {!isChat && (
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', color: 'var(--text-muted)', fontSize: 11 }}>
          <span>msgs: {session.message_count}</span>
          <span>·</span>
          <span>mem: {session.memory_files_count}</span>
          <span>·</span>
          <span>{humanBytes(session.db_row_bytes)}</span>
        </div>
      )}

      {session.task_preview && !isChat && (
        <div
          title={session.task_preview}
          style={{
            color: 'var(--text-primary)',
            fontSize: 12,
            opacity: 0.85,
            maxHeight: 64,
            overflowY: 'auto',
            overflowX: 'hidden',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            paddingRight: 4,
            scrollbarWidth: 'thin',
          }}
        >
          {session.task_preview}
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, marginTop: 2 }}>
        <CopyButton label="Copy chat resume" command={chatResumeCmd} />
        <CopyButton label="Copy CLI resume" command={cliResumeCmd} accent="rgba(255,200,80,0.95)" />
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
