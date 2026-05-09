import React from 'react'
import ProposalCard from './ProposalCard'
import AskCard from './AskCard'

export default function ProposalsPanel({ proposals, asks, onRemoveProposal, onRemoveAsk }) {
  const proposalList = [...proposals.values()]
  const askList = [...asks.values()]
  const isEmpty = proposalList.length === 0 && askList.length === 0

  return (
    <div style={{
      flex: 1,
      overflowY: 'auto',
      padding: '20px 24px',
      display: 'flex',
      flexDirection: 'column',
      gap: 12,
    }}>
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        marginBottom: 4,
      }}>
        <h2 style={{
          fontSize: 13,
          fontWeight: 600,
          fontFamily: 'var(--font-mono)',
          color: 'var(--text-primary)',
          textTransform: 'uppercase',
          letterSpacing: '0.1em',
        }}>
          Proposals
        </h2>
        {proposalList.length + askList.length > 0 && (
          <span style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            color: 'var(--neon-green)',
            background: 'rgba(0,255,136,0.08)',
            border: '1px solid rgba(0,255,136,0.2)',
            borderRadius: 10,
            padding: '1px 7px',
          }}>
            {proposalList.length + askList.length}
          </span>
        )}
      </div>

      {isEmpty && (
        <div style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 12,
          color: 'var(--text-muted)',
          fontFamily: 'var(--font-mono)',
          fontSize: 12,
        }}>
          <div style={{ fontSize: 32, opacity: 0.3 }}>⚡</div>
          <div>{'> no pending proposals'}</div>
          <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
            New file events will appear here for your approval.
          </div>
        </div>
      )}

      {/* Permission asks first (higher urgency) */}
      {askList.map(ask => (
        <AskCard key={ask.ask_id} ask={ask} onResolved={onRemoveAsk} />
      ))}

      {/* Proposals */}
      {proposalList.map(p => (
        <ProposalCard key={p.proposal_id} proposal={p} onResolved={onRemoveProposal} />
      ))}
    </div>
  )
}
