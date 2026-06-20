import React, { useState } from 'react'
import { respondAsk } from '../../api/client'

// Defensive: the server now formats ask bodies into readable text, but if a body
// still arrives as a raw JSON object/string, render it as indented key lines
// instead of a one-line blob. Plain strings pass through unchanged.
function prettyBody(body) {
  if (body == null) return ''
  if (typeof body !== 'string') {
    try { return JSON.stringify(body, null, 2) } catch { return String(body) }
  }
  const trimmed = body.trim()
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try { return JSON.stringify(JSON.parse(trimmed), null, 2) } catch { /* not JSON */ }
  }
  return body
}

// Consent option metadata: nicer labels + whether the option is affirmative
// (green) vs. a rejection (red). Scope options (session/project) map to the
// Claude/Cursor-style "allow for this session/project" allowlist choices.
const OPTION_META = {
  approve: { label: 'Approve once', affirmative: true },
  session: { label: 'Allow for session', affirmative: true },
  project: { label: 'Allow for project', affirmative: true },
  reject: { label: 'Reject', affirmative: false },
}

function optMeta(opt) {
  const key = String(opt).toLowerCase()
  if (OPTION_META[key]) return OPTION_META[key]
  // Fallback for legacy/custom options.
  const affirmative = /allow|approve|yes|session|project/.test(key)
  return { label: opt, affirmative }
}

export default function AskCard({ ask, onResolved }) {
  const [loading, setLoading] = useState(null)
  const [freeText, setFreeText] = useState('')

  async function respond(response) {
    setLoading(response)
    try {
      await respondAsk(ask.ask_id, response)
      onResolved(ask.ask_id)
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(null)
    }
  }

  const hasOptions = ask.options && ask.options.length > 0
  // Background subagents tag their agent_path with `#bg`. Show a clear badge
  // so users can tell a question is from a background worker (vs the master
  // agent or a sync subagent).
  const isBackground = !!(ask.agent_path && ask.agent_path.endsWith('#bg'))

  const btnBase = {
    padding: '6px 14px',
    borderRadius: 'var(--radius-btn)',
    fontSize: 11,
    fontFamily: 'var(--font-mono)',
    fontWeight: 600,
    letterSpacing: '0.05em',
    border: '1px solid',
    cursor: 'pointer',
    transition: 'all 0.15s',
  }

  return (
    <div style={{
      background: 'var(--bg-card)',
      border: '1px solid var(--border-amber)',
      borderRadius: 'var(--radius-card)',
      padding: '14px 16px',
      display: 'flex',
      flexDirection: 'column',
      gap: 10,
      animation: 'card-enter 0.2s ease',
      boxShadow: 'var(--shadow-card)',
      animationName: 'neon-pulse-amber',
      animationDuration: '2s',
      animationTimingFunction: 'ease-in-out',
      animationIterationCount: 'infinite',
    }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          color: 'var(--neon-amber)',
          background: 'rgba(251,191,36,0.08)',
          border: '1px solid rgba(251,191,36,0.25)',
          borderRadius: 3,
          padding: '2px 6px',
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
        }}>
          Permission
        </span>
        {isBackground && (
          <span
            title="Question from a background subagent"
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
              color: 'var(--neon-cyan, #22d3ee)',
              background: 'rgba(34,211,238,0.08)',
              border: '1px solid rgba(34,211,238,0.30)',
              borderRadius: 3,
              padding: '2px 6px',
              textTransform: 'uppercase',
              letterSpacing: '0.08em',
            }}
          >
            Background
          </span>
        )}
        <span style={{ color: 'var(--text-primary)', fontWeight: 600, fontSize: 13 }}>
          {ask.title || 'Permission request'}
        </span>
        {ask.agent_path && (
          <span
            title={ask.agent_path}
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
              color: 'var(--text-secondary)',
              background: 'rgba(255,255,255,0.04)',
              border: '1px solid rgba(255,255,255,0.08)',
              borderRadius: 3,
              padding: '2px 6px',
            }}
          >
            {ask.agent_path.split('/').slice(-2).join('/')}
          </span>
        )}
        {ask.session_id && (
          <span
            title={ask.session_id}
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
              color: 'var(--text-dim)',
            }}
          >
            sess: {String(ask.session_id).slice(-8)}
          </span>
        )}
      </div>

      {/* Body */}
      <pre className="selectable" style={{
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        lineHeight: 1.6,
        color: 'var(--text-secondary)',
        background: 'rgba(0,0,0,0.4)',
        border: '1px solid rgba(251,191,36,0.1)',
        borderRadius: 4,
        padding: '8px 10px',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        margin: 0,
      }}>
        {prettyBody(ask.body)}
      </pre>

      {/* Response */}
      {hasOptions ? (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {ask.options.map(opt => {
            const meta = optMeta(opt)
            return (
              <button
                key={opt}
                onClick={() => respond(opt)}
                disabled={!!loading}
                style={{
                  ...btnBase,
                  color: meta.affirmative ? 'var(--neon-green)' : 'var(--neon-red)',
                  borderColor: meta.affirmative ? 'rgba(0,255,136,0.3)' : 'rgba(255,51,102,0.3)',
                  background: meta.affirmative ? 'rgba(0,255,136,0.06)' : 'rgba(255,51,102,0.06)',
                }}
              >
                {loading === opt ? '...' : meta.label}
              </button>
            )
          })}
        </div>
      ) : (
        <div style={{ display: 'flex', gap: 8 }}>
          <input
            type="text"
            value={freeText}
            onChange={e => setFreeText(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && freeText.trim()) respond(freeText.trim()) }}
            placeholder="Type response and press Enter…"
            disabled={!!loading}
            style={{ flex: 1, fontFamily: 'var(--font-mono)', fontSize: 11 }}
          />
          <button
            onClick={() => respond(freeText.trim() || 'reject')}
            disabled={!!loading}
            style={{ ...btnBase, color: 'var(--neon-green)', borderColor: 'rgba(0,255,136,0.3)', background: 'rgba(0,255,136,0.06)' }}
          >
            Send
          </button>
        </div>
      )}
    </div>
  )
}
