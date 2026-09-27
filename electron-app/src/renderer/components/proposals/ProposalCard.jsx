import React, { useState } from 'react'
import CountdownBadge from './CountdownBadge'
import { respondProposal } from '../../api/client'
import './ProposalCard.css'

const URGENCY_DOTS = (n) =>
  Array.from({ length: 3 }, (_, i) => (
    <span key={i} className={`proposal-dot${i < n ? ' proposal-dot--on' : ''}`} />
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

  return (
    <div className="hover-bulge proposal">
      {/* Header row */}
      <div className="proposal-head">
        <div className="proposal-tags">
          <span className="proposal-agent">{proposal.subagent || 'agent'}</span>
          <span className="proposal-topic">{proposal.topic}</span>
          {proposal.agent_path && (
            <span title={proposal.agent_path} className="proposal-path">
              {proposal.agent_path.split('/').slice(-2).join('/')}
            </span>
          )}
          {proposal.session_id && (
            <span title={proposal.session_id} className="proposal-session">
              sess: {String(proposal.session_id).slice(-8)}
            </span>
          )}
        </div>
        <div className="proposal-urgency">{URGENCY_DOTS(proposal.urgency || 0)}</div>
      </div>

      {/* Summary */}
      <p className="proposal-summary">{proposal.summary}</p>

      {/* Proposed instruction block */}
      <div>
        <div className="proposal-label">Proposed instruction</div>
        {modifying ? (
          <textarea
            className="selectable proposal-edit"
            value={editedText}
            onChange={(e) => setEditedText(e.target.value)}
            rows={3}
          />
        ) : (
          <div className="selectable proposal-instruction">
            <span className="proposal-prompt">$ </span>
            {proposal.proposed}
          </div>
        )}
      </div>

      {/* Action buttons + countdown */}
      <div className="proposal-foot">
        <div className="proposal-actions">
          {!modifying ? (
            <>
              <button
                onClick={() => respond('approve')}
                disabled={!!loading}
                className="proposal-btn proposal-btn--approve"
              >
                {loading === 'approve' ? '...' : 'Approve'}
              </button>
              <button
                onClick={() => respond('approve_remember')}
                disabled={!!loading}
                className="proposal-btn proposal-btn--remember proposal-btn--small"
              >
                {loading === 'approve_remember' ? '...' : 'Remember'}
              </button>
              <button
                onClick={() => setModifying(true)}
                disabled={!!loading}
                className="proposal-btn proposal-btn--modify"
              >
                Modify
              </button>
              <button
                onClick={() => respond('skip')}
                disabled={!!loading}
                className="proposal-btn proposal-btn--skip"
              >
                {loading === 'skip' ? '...' : 'Skip'}
              </button>
              <button
                onClick={() => respond('skip_remember')}
                disabled={!!loading}
                className="proposal-btn proposal-btn--quiet proposal-btn--small"
              >
                Forget
              </button>
            </>
          ) : (
            <>
              <button
                onClick={() => respond('modify', editedText)}
                disabled={!!loading}
                className="proposal-btn proposal-btn--approve"
              >
                {loading === 'modify' ? '...' : 'Submit'}
              </button>
              <button
                onClick={() => setModifying(false)}
                className="proposal-btn proposal-btn--quiet"
              >
                Cancel
              </button>
            </>
          )}
        </div>

        <div className="proposal-expires">
          expires&nbsp;
          <CountdownBadge expiresTs={proposal.expires_ts} />
        </div>
      </div>
    </div>
  )
}
