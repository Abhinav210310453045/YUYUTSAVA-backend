import React, { useState } from 'react'
import { respondAsk } from '../../api/client'

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
        <span style={{ color: 'var(--text-primary)', fontWeight: 600, fontSize: 13 }}>
          {ask.title}
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
        {ask.body}
      </pre>

      {/* Response */}
      {hasOptions ? (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {ask.options.map(opt => (
            <button
              key={opt}
              onClick={() => respond(opt)}
              disabled={!!loading}
              style={{
                ...btnBase,
                color: opt.toLowerCase().includes('allow') ? 'var(--neon-green)' : 'var(--neon-red)',
                borderColor: opt.toLowerCase().includes('allow') ? 'rgba(0,255,136,0.3)' : 'rgba(255,51,102,0.3)',
                background: opt.toLowerCase().includes('allow') ? 'rgba(0,255,136,0.06)' : 'rgba(255,51,102,0.06)',
              }}
            >
              {loading === opt ? '...' : opt}
            </button>
          ))}
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
