import React from 'react'
import { blockSrc } from './src'

function humanSize(bytes) {
  if (typeof bytes !== 'number' || !isFinite(bytes)) return null
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// Fallback for every attachment no registered block claims (video, generic
// artifact, future kinds): a tile naming the file with a Download action.
export default function DownloadTile({ attachment, cardId }) {
  const size = humanSize(attachment.meta?.size)
  const name = attachment.title || attachment.meta?.filename
    || (attachment.path || '').split('/').pop() || attachment.attachment_id
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px',
      background: 'var(--bg-card)', border: '1px solid var(--border-card)',
      borderRadius: 6, fontFamily: 'var(--font-mono)', fontSize: 11,
    }}>
      <span style={{ fontSize: 16 }}>📄</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          color: 'var(--text-primary)', overflow: 'hidden',
          textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {name}
        </div>
        <div style={{ color: 'var(--text-dim)', fontSize: 10 }}>
          {attachment.kind}{attachment.mime ? ` · ${attachment.mime}` : ''}{size ? ` · ${size}` : ''}
        </div>
      </div>
      <a
        href={blockSrc(attachment, cardId, { download: true })}
        download
        style={{
          fontSize: 10, padding: '3px 9px', color: 'var(--neon-green)',
          border: '1px solid rgba(var(--accent-rgb),0.25)', borderRadius: 6,
          textDecoration: 'none', whiteSpace: 'nowrap',
        }}
      >
        Download
      </a>
    </div>
  )
}
