import React, { useEffect, useRef } from 'react'
import ProposalCard from './ProposalCard'
import AskCard from './AskCard'
import { useNotifications } from '../../hooks/useNotifications.jsx'

export default function ProposalsPanel({ proposals, asks, onRemoveProposal, onRemoveAsk }) {
  const proposalList = [...proposals.values()]
  const askList = [...asks.values()]
  const isEmpty = proposalList.length === 0 && askList.length === 0

  // When the user clicks an OS notification banner, NotificationsProvider sets
  // highlightId; scroll the matching card into view and add a flash class.
  const { highlightId } = useNotifications() || {}
  const cardRefs = useRef(new Map())

  useEffect(() => {
    if (!highlightId) return
    const el = cardRefs.current.get(highlightId)
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el.classList.add('proposal-flash')
    const t = setTimeout(() => el.classList.remove('proposal-flash'), 1800)
    return () => clearTimeout(t)
  }, [highlightId])

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
          fontWeight: 'var(--fw-semibold)',
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
            background: 'rgba(var(--accent-rgb),0.08)',
            border: '1px solid rgba(var(--accent-rgb),0.2)',
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
        <div
          key={ask.ask_id}
          ref={(el) => {
            if (el) cardRefs.current.set(ask.ask_id, el)
            else cardRefs.current.delete(ask.ask_id)
          }}
        >
          <AskCard ask={ask} onResolved={onRemoveAsk} />
        </div>
      ))}

      {/* Proposals */}
      {proposalList.map(p => (
        <div
          key={p.proposal_id}
          ref={(el) => {
            if (el) cardRefs.current.set(p.proposal_id, el)
            else cardRefs.current.delete(p.proposal_id)
          }}
        >
          <ProposalCard proposal={p} onResolved={onRemoveProposal} />
        </div>
      ))}
    </div>
  )
}
