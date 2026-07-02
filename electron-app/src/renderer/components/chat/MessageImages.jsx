import React, { useState } from 'react'
import { visualUrl } from '../../api/client'
import { kindAccent } from '../artifacts/kinds'
import Lightbox from '../artifacts/Lightbox'
import VisualActions from '../artifacts/VisualActions'

// Inline rendering of a message's rendered artifacts (charts/diagrams/tables/…)
// right inside the chat/voice bubble — Claude-Desktop style. Click to zoom.
// `images` is the message's `images` array from useConverse.
export default function MessageImages({ images }) {
  const [zoomed, setZoomed] = useState(null)
  // Deleting an inline artifact removes it from disk/DB; hide it here too so the
  // bubble doesn't keep a now-broken <img> around.
  const [hidden, setHidden] = useState(() => new Set())
  const onDeleted = (id) => setHidden((cur) => new Set(cur).add(id))
  if (!images || images.length === 0) return null
  const shown = images.filter((img) => !hidden.has(img.visual_id))
  if (shown.length === 0) return null

  return (
    <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 10 }}>
      {shown.map((img) => {
        const accent = kindAccent(img.kind)
        return (
          <figure
            key={img.visual_id}
            className="hover-bulge"
            style={{
              margin: 0, borderRadius: 12, overflow: 'hidden',
              border: `1px solid ${accent}44`,
              background: 'var(--bg-deep)',
              boxShadow: `0 2px 14px rgba(0,0,0,0.4), 0 0 18px ${accent}18`,
              '--bulge-glow': `${accent}55`,
              animation: 'bubble-pop 0.28s cubic-bezier(0.34,1.56,0.64,1)',
              maxWidth: 380,
            }}
          >
            <img
              src={visualUrl(img.url)} alt={img.title || img.kind} loading="lazy"
              onClick={() => setZoomed(img)}
              style={{ display: 'block', width: '100%', maxHeight: 260, objectFit: 'contain', background: 'var(--bg-deep)', cursor: 'zoom-in' }}
            />
            <figcaption style={{
              display: 'flex', alignItems: 'center', gap: 6, padding: '6px 10px',
              borderTop: `1px solid ${accent}22`,
            }}>
              <span style={{
                fontFamily: 'var(--font-mono)', fontSize: 9, textTransform: 'uppercase',
                letterSpacing: '0.06em', color: accent, border: `1px solid ${accent}55`,
                borderRadius: 6, padding: '1px 6px',
              }}>{img.kind}</span>
              <span style={{
                fontSize: 11, color: 'var(--text-secondary)', overflow: 'hidden',
                whiteSpace: 'nowrap', textOverflow: 'ellipsis', flex: 1,
              }}>{img.title || 'visual'}</span>
              <VisualActions visual={img} onDeleted={onDeleted} />
            </figcaption>
          </figure>
        )
      })}
      <Lightbox v={zoomed} onClose={() => setZoomed(null)} onDeleted={onDeleted} />
    </div>
  )
}
