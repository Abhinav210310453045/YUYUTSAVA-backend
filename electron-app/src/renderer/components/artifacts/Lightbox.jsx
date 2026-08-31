import React, { useEffect } from 'react'
import { visualUrl } from '../../api/client'
import VisualActions from './VisualActions'

// Full-screen zoom overlay for a rendered visual. Shared by the Artifacts
// gallery and inline chat/voice image cards. Click anywhere or press Esc to
// close. `v` is a visual record ({ visual_id, url, title, kind, mime }); null =
// hidden. A floating Copy/Download/Delete toolbar sits over the image.
export default function Lightbox({ v, onClose, onDeleted }) {
  useEffect(() => {
    if (!v) return
    const onKey = (e) => { if (e.key === 'Escape') onClose?.() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [v, onClose])

  if (!v) return null
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.88)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 200, padding: 40, cursor: 'zoom-out',
        animation: 'fade-in 0.15s ease', backdropFilter: 'blur(4px)',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          position: 'relative', display: 'flex', flexDirection: 'column', gap: 12,
          maxWidth: '100%', maxHeight: '100%', alignItems: 'center', cursor: 'default',
        }}
      >
        <div style={{ alignSelf: 'flex-end' }}>
          <VisualActions visual={v} onDeleted={(id) => { onDeleted?.(id); onClose?.() }} dark />
        </div>
        <img
          src={visualUrl(v.url)} alt={v.title || v.kind}
          style={{
            maxWidth: '100%', maxHeight: 'calc(100vh - 140px)', objectFit: 'contain',
            borderRadius: 10, boxShadow: '0 12px 60px rgba(0,0,0,0.7)',
            animation: 'overlay-in 0.2s ease',
          }}
        />
      </div>
    </div>
  )
}
