import React from 'react'
import { TagChips, humanAge } from '../shared'

// One line per todo_events row — the frontend twin of the journey document's
// timeline humanizer (block_journey.py).
function humanizeEvent(e) {
  const p = e.payload || {}
  const title = p.title || ''
  switch (e.kind) {
    case 'card_status':
      return `card moved ${p.from} → ${p.to}`
    case 'objective_created':
      return `objective added: “${title}” [${p.phase}]`
    case 'objective_phase':
      return `“${title}”: ${p.from} → ${p.to}${p.reason ? ` — ${p.reason}` : ''}`
    case 'objective_updated':
      return `“${title}”: ${(p.fields || []).join(', ')} changed`
    case 'objective_deleted':
      return `objective removed: “${title}”`
    case 'note_assigned':
      return 'a note was assigned to an objective'
    case 'artifact_attached':
      return `attached ${p.kind}${title ? `: ${title}` : ''}`
    case 'journey_generated':
      return 'journey document generated'
    default:
      return e.kind
  }
}

// Under the header: tags, the card's ages, and the Activity toggle with its
// timeline.
export default function CardMeta({ card, activityOpen, onToggleActivity, events }) {
  return (
    <>
      {/* Meta strip: tags, timestamps, activity toggle. */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '10px 24px 0',
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          color: 'var(--text-muted)',
          flexShrink: 0,
        }}
      >
        <TagChips tags={card.tags} />
        <span style={{ flex: 1 }} />
        <button
          onClick={onToggleActivity}
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            padding: '2px 8px',
            background: 'transparent',
            color: 'var(--text-muted)',
            border: '1px solid var(--border-card)',
            borderRadius: 6,
            cursor: 'pointer',
          }}
        >
          {activityOpen ? '▾ Activity' : '▸ Activity'}
        </button>
        <span>created {humanAge(card.created_ts)}</span>
        <span style={{ color: 'var(--text-dim)' }}>·</span>
        <span>updated {humanAge(card.updated_ts)}</span>
      </div>

      {activityOpen && (
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 4,
            fontFamily: 'var(--font-mono)',
            fontSize: 11,
            borderLeft: '2px solid var(--border-card)',
            margin: '8px 24px 0',
            paddingLeft: 12,
            maxHeight: 140,
            overflowY: 'auto',
            flexShrink: 0,
          }}
        >
          {events.length === 0 && (
            <span style={{ color: 'var(--text-dim)' }}>{'> no activity recorded yet'}</span>
          )}
          {events.map((e) => (
            <div key={e.event_id} style={{ display: 'flex', gap: 8, color: 'var(--text-muted)' }}>
              <span style={{ color: 'var(--text-dim)', whiteSpace: 'nowrap' }}>
                {humanAge(e.created_ts)}
              </span>
              <span style={{ color: 'var(--text-info)' }}>[{e.actor}]</span>
              <span style={{ minWidth: 0, wordBreak: 'break-word' }}>{humanizeEvent(e)}</span>
            </div>
          ))}
        </div>
      )}
    </>
  )
}
