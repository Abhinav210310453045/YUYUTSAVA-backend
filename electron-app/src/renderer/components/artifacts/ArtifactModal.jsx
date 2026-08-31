import React, { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { resolveBlock } from '../todos/artifactBlocks'

// Full-screen "big view" for any artifact — the block-aware twin of Lightbox
// (which is <img>-only). Renders whatever the pluggable registry resolves for
// this attachment, in its `expanded` layout, so a JSX sandbox / audio clip /
// text file / diagram all open large. Shared by the TODO board and inline
// chat/voice. Esc or the collapse (⤡) / backdrop click returns to normal view.
//
// `attachment` is a TodoAttachmentV1 (card context, pass cardId) or a general
// artifact record (chat context, no cardId — the block resolves its bytes URL
// from the record's `url`). null = hidden.

const KIND_ACCENT = {
  image: 'var(--text-info)', diagram: 'var(--text-info)', artifact: 'var(--text-info)',
  link: 'var(--text-lavender)', file: '#8fa1c7', video: '#f0a3c0',
}

export default function ArtifactModal({ attachment, cardId, onClose }) {
  useEffect(() => {
    if (!attachment) return
    const onKey = (e) => { if (e.key === 'Escape') onClose?.() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [attachment, onClose])

  if (!attachment) return null
  const Block = resolveBlock(attachment)
  const accent = KIND_ACCENT[attachment.kind] || 'var(--text-info)'

  // Portal to <body>: mount points (chat bubbles, card views) carry
  // backdrop-filter / hover transforms, which make the ancestor the containing
  // block for position:fixed — the overlay would anchor to the bubble instead
  // of the viewport.
  return createPortal(
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.88)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 200, padding: 32, animation: 'fade-in 0.15s ease',
        backdropFilter: 'blur(4px)',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          position: 'relative', display: 'flex', flexDirection: 'column',
          width: 'min(1100px, 96vw)', maxHeight: '92vh',
          background: 'var(--bg-elevated, #14141f)',
          border: `1px solid ${accent}44`, borderRadius: 12,
          boxShadow: `0 16px 70px rgba(0,0,0,0.7), 0 0 30px ${accent}18`,
          animation: 'overlay-in 0.2s ease', overflow: 'hidden',
        }}
      >
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '10px 14px', borderBottom: `1px solid ${accent}22`,
          fontFamily: 'var(--font-mono)',
        }}>
          <span style={{
            fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.06em',
            color: accent, border: `1px solid ${accent}55`, borderRadius: 6,
            padding: '1px 6px',
          }}>{attachment.kind}</span>
          <span style={{
            flex: 1, minWidth: 0, fontSize: 12, color: 'var(--text-secondary)',
            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
          }}>{attachment.title || attachment.mime || 'artifact'}</span>
          <button
            onClick={onClose}
            title="collapse (Esc)"
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              background: 'transparent', color: 'var(--text-muted)',
              border: '1px solid var(--border-card)', borderRadius: 6,
              padding: '4px 10px', cursor: 'pointer', fontSize: 11,
              fontFamily: 'var(--font-mono)',
            }}
          >⤡ collapse</button>
        </div>
        <div style={{ padding: 16, overflow: 'auto', minWidth: 0 }}>
          <Block attachment={attachment} cardId={cardId} expanded />
        </div>
      </div>
    </div>,
    document.body,
  )
}
