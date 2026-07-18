import React from 'react'
import { blockSrc } from './src'

export const matches = (att) => att.kind === 'image'

export default function ImageBlock({ attachment, cardId, expanded }) {
  return (
    <img
      src={blockSrc(attachment, cardId)}
      alt={attachment.title || attachment.kind}
      style={{
        display: 'block', maxWidth: '100%', maxHeight: expanded ? '82vh' : 260,
        objectFit: 'contain', borderRadius: 6,
        background: 'var(--bg-card)',
      }}
    />
  )
}
