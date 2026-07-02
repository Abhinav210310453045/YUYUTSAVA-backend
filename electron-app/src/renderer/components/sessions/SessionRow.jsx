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

// Per-origin accent used for the card's left bar, border tint, and glow so the
// three columns are distinguishable at a glance. Keyed by session.origin.
const ORIGIN_ACCENT = {
  ui:    { bar: '#00ff88', border: 'rgba(0, 255, 136, 0.30)', glow: 'rgba(0, 255, 136, 0.10)', hover: 'rgba(0, 255, 136, 0.28)' },
  voice: { bar: '#7aa2ff', border: 'rgba(120, 160, 255, 0.34)', glow: 'rgba(120, 160, 255, 0.12)', hover: 'rgba(120, 160, 255, 0.32)' },
  cli:   { bar: '#fbbf24', border: 'rgba(251, 191, 36, 0.30)', glow: 'rgba(251, 191, 36, 0.10)', hover: 'rgba(251, 191, 36, 0.28)' },
}

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

export default function SessionRow({ session, onDeleted, onOpenSession }) {
  const [deleting, setDeleting] = useState(false)
  const [idExpanded, setIdExpanded] = useState(false)

  const shortId = session.id.length > 8 ? `${session.id.slice(0, 8)}…` : session.id
  const wsBase = session.workspace.split('/').filter(Boolean).pop() || session.workspace
  const dotColor = STATUS_COLOR[session.status] || 'var(--text-muted)'
  // origin is the DB-backed discriminator: UI chats & Voice convos resume in-app,
  // CLI rows expose copy-resume commands for the terminal. Voice rows get both —
  // click→continue-in-UI *and* a copy-resume-in-CLI button.
  const isUi = session.origin === 'ui'
  const isVoice = session.origin === 'voice'
  const isResumableInUi = isUi || isVoice
  const isChat = isResumableInUi || (session.task_preview || '').trim() === CHAT_PREVIEW_MARKER
  const accent = ORIGIN_ACCENT[session.origin] || ORIGIN_ACCENT.cli
  // Pass the whole session so the parent can route by origin (voice→Voice panel,
  // ui/chat→Chat panel) instead of always opening chat.
  const onRowClick = isResumableInUi ? () => onOpenSession?.(session) : undefined

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
    <div
      className="hover-bulge"
      onClick={onRowClick}
      title={isResumableInUi ? 'click to continue this conversation in the UI' : undefined}
      style={{
        // Elevated above the page so cards read as distinct surfaces, with a
        // per-origin left accent bar + tinted border + soft glow (green=chat,
        // blue=voice, amber=cli). --bulge-glow drives the hover glow (globals.css).
        background: 'var(--bg-elevated, #1a1a2e)',
        border: `1px solid ${accent.border}`,
        borderLeft: `3px solid ${accent.bar}`,
        borderRadius: 8,
        boxShadow: `0 2px 10px rgba(0, 0, 0, 0.45), 0 0 16px ${accent.glow}`,
        '--bulge-glow': accent.hover,
        padding: '12px 14px',
        fontFamily: 'var(--font-mono)',
        fontSize: 12,
        color: 'var(--text-primary)',
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        cursor: isResumableInUi ? 'pointer' : 'default',
      }}
    >
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
          onClick={(e) => { e.stopPropagation(); setIdExpanded((v) => !v) }}
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
            {isVoice ? 'voice' : 'chat'}
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
        {isResumableInUi ? (
          <>
            <button
              onClick={(e) => { e.stopPropagation(); onOpenSession?.(session) }}
              style={{
                flex: 1,
                fontFamily: 'var(--font-mono)',
                fontSize: 11,
                padding: '6px 10px',
                background: 'rgba(120,160,255,0.10)',
                color: '#9bb8ff',
                border: '1px solid rgba(120,160,255,0.3)',
                borderRadius: 6,
                cursor: 'pointer',
                whiteSpace: 'nowrap',
              }}
            >
              Continue in UI
            </button>
            {/* Voice convos can also be resumed from the terminal as a chat. */}
            {isVoice && (
              <CopyButton label="Copy CLI resume" command={chatResumeCmd} accent="rgba(255,200,80,0.95)" />
            )}
          </>
        ) : (
          <>
            <CopyButton label="Copy chat resume" command={chatResumeCmd} />
            <CopyButton label="Copy CLI resume" command={cliResumeCmd} accent="rgba(255,200,80,0.95)" />
          </>
        )}
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
