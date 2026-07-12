import React from 'react'
import { todoAttachmentUrl } from '../../../api/client'

export const matches = (att) => att.kind === 'image'

export default function ImageBlock({ attachment, cardId }) {
  return (
    <img
      src={todoAttachmentUrl(cardId, attachment.attachment_id)}
      alt={attachment.title || attachment.kind}
      style={{
        display: 'block', maxWidth: '100%', maxHeight: 260,
        objectFit: 'contain', borderRadius: 6,
        background: 'var(--bg-card)',
      }}
    />
  )
}
