import React, { useEffect, useRef } from 'react'
import ProposalCard from './ProposalCard'
import AskCard from '../asks/AskCard'
import { useAsks } from '../../hooks/useAsks.jsx'
import { useAskRouting } from '../../hooks/useAskRouting'
import { useNotifications } from '../../hooks/useNotifications.jsx'

// The Inbox: everything waiting on the user, in one place.
//
// Asks live here *unconditionally* while they are pending — whether they were
// raised by the chat you have open, a background task, or an agent whose
// daemon has since restarted. That is the point: an ask never expires and must
// never be something you can only reach by being in the right view. The card is
// the same one the owning view and the overlay use, so answering here is
// identical to answering there and every surface clears together.

function SectionHeading({ title, count, hint }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 4 }}>
      <h2 style={{
        fontSize: 13,
        fontWeight: 'var(--fw-semibold)',
        fontFamily: 'var(--font-mono)',
        color: 'var(--text-primary)',
        textTransform: 'uppercase',
        letterSpacing: '0.1em',
      }}>
        {title}
      </h2>
      {count > 0 && (
        <span style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          color: 'var(--neon-green)',
          background: 'rgba(var(--accent-rgb),0.08)',
          border: '1px solid rgba(var(--accent-rgb),0.2)',
          borderRadius: 10,
          padding: '1px 7px',
        }}>
          {count}
        </span>
      )}
      {hint && (
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-dim)',
        }}>{hint}</span>
      )}
    </div>
  )
}

export default function ProposalsPanel({ proposals, onRemoveProposal }) {
  const proposalList = [...proposals.values()]
  // One source of truth for asks: hydrated from GET /asks on connect (so a
  // dropped SSE frame or a daemon restart can't hide one) and kept live by the
  // stream.
  const { asks, answer, answering } = useAsks()
  const { isOnOwningView, goToAsk } = useAskRouting(asks)
  const isEmpty = proposalList.length === 0 && asks.length === 0

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
      <SectionHeading
        title="Inbox"
        count={asks.length}
        hint={asks.length > 0 ? 'agents are waiting on you' : null}
      />

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
          <div>{'> nothing waiting on you'}</div>
          <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
            Permission requests and agent questions land here until you answer them.
          </div>
        </div>
      )}

      {/* Asks — always listed while pending, whoever owns them. */}
      {asks.map((ask) => (
        <div
          key={ask.ask_id}
          ref={(el) => {
            if (el) cardRefs.current.set(ask.ask_id, el)
            else cardRefs.current.delete(ask.ask_id)
          }}
          style={{ display: 'flex', flexDirection: 'column', gap: 4 }}
        >
          <AskCard
            ask={ask}
            onAnswer={answer}
            answering={answering === ask.ask_id}
            headerAction={!isOnOwningView(ask) && ask.surface !== 'background' ? (
              <button
                onClick={() => goToAsk(ask)}
                title="Open the conversation this belongs to"
                style={{
                  background: 'transparent', border: 'none', cursor: 'pointer',
                  color: 'var(--text-info)', fontFamily: 'var(--font-mono)',
                  fontSize: 9, letterSpacing: '0.08em', textTransform: 'uppercase',
                  padding: '2px 4px',
                }}
              >
                open ↗
              </button>
            ) : null}
          />
        </div>
      ))}

      {proposalList.length > 0 && (
        <SectionHeading title="Proposals" count={proposalList.length} />
      )}
      {proposalList.map((p) => (
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
