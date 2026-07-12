import React from 'react'
import { todoAttachmentUrl } from '../../../api/client'

// vis_* renders (diagram/chart/code-image) land as PNG/SVG files with kind
// "diagram" — visually an image, but a distinct block so it can grow
// diagram-specific affordances (source view, re-render) without touching
// ImageBlock.
export const matches = (att) => att.kind === 'diagram'

export default function DiagramBlock({ attachment, cardId }) {
  return (
    <img
      src={todoAttachmentUrl(cardId, attachment.attachment_id)}
      alt={attachment.title || 'diagram'}
      style={{
        display: 'block', maxWidth: '100%', maxHeight: 260,
        objectFit: 'contain', borderRadius: 6,
        background: '#fff', // Kroki/mpl output assumes a light canvas
      }}
    />
  )
}
