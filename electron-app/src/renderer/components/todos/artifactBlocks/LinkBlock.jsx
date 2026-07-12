import React from 'react'

export const matches = (att) => att.kind === 'link'

export default function LinkBlock({ attachment }) {
  return (
    <a
      href={attachment.url}
      target="_blank"
      rel="noreferrer"
      style={{
        display: 'block', padding: '10px 12px',
        background: 'var(--bg-card)', border: '1px solid rgba(120,160,255,0.25)',
        borderRadius: 6, fontFamily: 'var(--font-mono)', fontSize: 11,
        color: '#9bb8ff', textDecoration: 'none',
        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
      }}
      title={attachment.url}
    >
      🔗 {attachment.title || attachment.url}
    </a>
  )
}
