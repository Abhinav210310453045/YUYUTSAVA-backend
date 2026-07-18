import React, { useState } from 'react'
import { resolveBlock } from '../todos/artifactBlocks'
import ArtifactModal from '../artifacts/ArtifactModal'

// Inline rendering of a message's rich artifacts (interactive HTML/JSX, docs,
// audio) right inside the chat/voice bubble — the non-visual twin of
// MessageImages. Each renders through the shared block registry (no cardId, so
// blocks resolve their bytes from the record's `url`) and opens to the big view
// (ArtifactModal) with a collapse button. `artifacts` is the message's
// `artifacts` array from useConverse.

const KIND_ACCENT = {
  image: 'var(--text-info)', diagram: 'var(--text-info)', artifact: 'var(--text-info)',
  link: 'var(--text-lavender)', file: '#8fa1c7', video: '#f0a3c0',
}
const CLICK_TO_EXPAND = new Set(['image', 'diagram'])

export default function MessageArtifacts({ artifacts }) {
  const [expanded, setExpanded] = useState(null)
  if (!artifacts || artifacts.length === 0) return null

  return (
    <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 10 }}>
      {artifacts.map((att) => {
        const Block = resolveBlock(att)
        const accent = KIND_ACCENT[att.kind] || 'var(--text-info)'
        const clickable = CLICK_TO_EXPAND.has(att.kind)
        return (
          <figure
            key={att.attachment_id}
            style={{
              margin: 0, borderRadius: 12, overflow: 'hidden',
              border: `1px solid ${accent}44`, background: 'var(--bg-deep)',
              boxShadow: `0 2px 14px rgba(0,0,0,0.4), 0 0 18px ${accent}18`,
              maxWidth: 440,
              animation: 'bubble-pop 0.28s cubic-bezier(0.34,1.56,0.64,1)',
            }}
          >
            <figcaption style={{
              display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px',
              borderBottom: `1px solid ${accent}22`, fontFamily: 'var(--font-mono)',
            }}>
              <span style={{
                fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.06em',
                color: accent, border: `1px solid ${accent}55`, borderRadius: 6,
                padding: '1px 6px',
              }}>{att.kind}</span>
              <span style={{
                fontSize: 11, color: 'var(--text-secondary)', overflow: 'hidden',
                whiteSpace: 'nowrap', textOverflow: 'ellipsis', flex: 1,
              }}>{att.title || att.mime || 'artifact'}</span>
              <button
                onClick={() => setExpanded(att)}
                title="open big view"
                style={{
                  background: 'transparent', color: accent, cursor: 'pointer',
                  border: `1px solid ${accent}55`, borderRadius: 6,
                  padding: '2px 8px', fontSize: 11, fontFamily: 'var(--font-mono)',
                }}
              >⤢</button>
            </figcaption>
            <div
              onClick={clickable ? () => setExpanded(att) : undefined}
              style={{ padding: 10, minWidth: 0, cursor: clickable ? 'zoom-in' : 'default' }}
            >
              <Block attachment={att} />
            </div>
          </figure>
        )
      })}
      <ArtifactModal attachment={expanded} onClose={() => setExpanded(null)} />
    </div>
  )
}
