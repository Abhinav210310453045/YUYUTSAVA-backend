import React, { useState } from 'react'
import CountdownBadge from './CountdownBadge'
import { respondProposal } from '../../api/client'

const URGENCY_DOTS = (n) => Array.from({ length: 3 }, (_, i) => (
  <span key={i} style={{
    width: 6, height: 6, borderRadius: '50%',
    background: i < n ? 'var(--neon-green)' : 'var(--border-card)',
    boxShadow: i < n ? '0 0 4px var(--neon-green)' : 'none',
    display: 'inline-block',
  }} />
))

export default function ProposalCard({ proposal, onResolved }) {
  const [loading, setLoading] = useState(null)
  const [modifying, setModifying] = useState(false)
  const [editedText, setEditedText] = useState(proposal.proposed)

  async function respond(decision, edited = null) {
    setLoading(decision)
    try {
      await respondProposal(proposal.proposal_id, decision, edited)
      onResolved(proposal.proposal_id)
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(null)
    }
  }

  const btnBase = {
    padding: '5px 12px',
    borderRadius: 'var(--radius-btn)',
    fontSize: 11,
    fontFamily: 'var(--font-mono)',
    fontWeight: 600,
    letterSpacing: '0.05em',
    border: '1px solid',
    cursor: 'pointer',
    transition: 'all 0.15s',
    textTransform: 'uppercase',
  }

  return (
    <div style={{
      background: 'var(--bg-card)',
      border: '1px solid var(--border-neon)',
      borderRadius: 'var(--radius-card)',
      padding: '14px 16px',
      display: 'flex',
      flexDirection: 'column',
      gap: 10,
      animation: 'card-enter 0.2s ease',
      boxShadow: 'var(--shadow-card)',
      backdropFilter: 'blur(8px)',
      animationName: 'neon-pulse',
      animationDuration: '3s',
      animationTimingFunction: 'ease-in-out',
      animationIterationCount: 'infinite',
    }}>
      {/* Header row */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <span style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            color: 'var(--neon-cyan)',
            background: 'rgba(0,212,255,0.08)',
            border: '1px solid rgba(0,212,255,0.2)',
            borderRadius: 3,
            padding: '2px 6px',
            textTransform: 'uppercase',
            letterSpacing: '0.08em',
          }}>
            {proposal.subagent || 'agent'}
          </span>
          <span style={{ color: 'var(--text-muted)', fontSize: 10, fontFamily: 'var(--font-mono)' }}>
            {proposal.topic}
          </span>
          {proposal.agent_path && (
            <span
              title={proposal.agent_path}
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
              {proposal.agent_path.split('/').slice(-2).join('/')}
            </span>
          )}
          {proposal.session_id && (
            <span title={proposal.session_id} style={{
              fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-dim)',
            }}>
              sess: {String(proposal.session_id).slice(-8)}
            </span>
          )}
        </div>
        <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
          {URGENCY_DOTS(proposal.urgency || 0)}
        </div>
      </div>

      {/* Summary */}
      <p style={{ color: 'var(--text-secondary)', fontSize: 12, lineHeight: 1.5 }}>
        {proposal.summary}
      </p>

      {/* Proposed instruction block */}
      <div>
        <div style={{
          fontSize: 9,
          fontFamily: 'var(--font-mono)',
          color: 'var(--text-muted)',
          textTransform: 'uppercase',
          letterSpacing: '0.1em',
          marginBottom: 6,
        }}>
          Proposed instruction
        </div>
        {modifying ? (
          <textarea
            className="selectable"
            value={editedText}
            onChange={e => setEditedText(e.target.value)}
            rows={3}
            style={{
              width: '100%',
              resize: 'vertical',
              fontFamily: 'var(--font-mono)',
              fontSize: 12,
              lineHeight: 1.6,
              color: 'var(--text-code)',
              background: 'rgba(0,0,0,0.4)',
              border: '1px solid var(--border-focus)',
              borderRadius: 4,
              padding: '8px 10px',
            }}
          />
        ) : (
          <div className="selectable" style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 12,
            lineHeight: 1.6,
            color: 'var(--text-code)',
            background: 'rgba(0,0,0,0.4)',
            border: '1px solid rgba(0,255,136,0.1)',
            borderRadius: 4,
            padding: '8px 10px',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}>
            <span style={{ color: 'var(--text-muted)' }}>$ </span>{proposal.proposed}
          </div>
        )}
      </div>

      {/* Action buttons + countdown */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 6 }}>
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
          {!modifying ? (
            <>
              <button onClick={() => respond('approve')} disabled={!!loading} style={{ ...btnBase, color: 'var(--neon-green)', borderColor: 'rgba(0,255,136,0.3)', background: 'rgba(0,255,136,0.06)' }}>
                {loading === 'approve' ? '...' : 'Approve'}
              </button>
              <button onClick={() => respond('approve_remember')} disabled={!!loading} style={{ ...btnBase, color: 'var(--neon-cyan)', borderColor: 'rgba(0,212,255,0.3)', background: 'rgba(0,212,255,0.06)', fontSize: 10 }}>
                {loading === 'approve_remember' ? '...' : 'Remember'}
              </button>
              <button onClick={() => setModifying(true)} disabled={!!loading} style={{ ...btnBase, color: 'var(--neon-purple)', borderColor: 'rgba(139,92,246,0.3)', background: 'rgba(139,92,246,0.06)' }}>
                Modify
              </button>
              <button onClick={() => respond('skip')} disabled={!!loading} style={{ ...btnBase, color: 'var(--neon-red)', borderColor: 'rgba(255,51,102,0.3)', background: 'rgba(255,51,102,0.06)' }}>
                {loading === 'skip' ? '...' : 'Skip'}
              </button>
              <button onClick={() => respond('skip_remember')} disabled={!!loading} style={{ ...btnBase, color: 'var(--text-muted)', borderColor: 'var(--border-card)', fontSize: 10 }}>
                Forget
              </button>
            </>
          ) : (
            <>
              <button
                onClick={() => respond('modify', editedText)}
                disabled={!!loading}
                style={{ ...btnBase, color: 'var(--neon-green)', borderColor: 'rgba(0,255,136,0.3)', background: 'rgba(0,255,136,0.06)' }}
              >
                {loading === 'modify' ? '...' : 'Submit'}
              </button>
              <button onClick={() => setModifying(false)} style={{ ...btnBase, color: 'var(--text-muted)', borderColor: 'var(--border-card)' }}>
                Cancel
              </button>
            </>
          )}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
          expires&nbsp;
          <CountdownBadge expiresTs={proposal.expires_ts} />
        </div>
      </div>
    </div>
  )
}
