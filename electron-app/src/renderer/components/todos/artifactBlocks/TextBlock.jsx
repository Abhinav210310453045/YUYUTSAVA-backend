import React, { useEffect, useState } from 'react'
import { blockSrc } from './src'

const TEXT_MIMES = ['text/plain', 'text/markdown', 'text/html', 'text/csv', 'application/json']
const PREVIEW_CHARS = 4000

export const matches = (att) =>
  att.kind === 'file' && TEXT_MIMES.includes(att.mime || '')

// Fetches the file's text and shows a scrollable plain-text preview (markdown
// and HTML render as source — never injected into the DOM). Expanded (big
// view) shows the full text in a tall pane; the tile preview stays clamped.
export default function TextBlock({ attachment, cardId, expanded }) {
  const [text, setText] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    fetch(blockSrc(attachment, cardId))
      .then((res) => {
        if (!res.ok) throw new Error(`fetch → ${res.status}`)
        return res.text()
      })
      .then((body) => { if (!cancelled) setText(body) })
      .catch((e) => { if (!cancelled) setError(e.message) })
    return () => { cancelled = true }
  }, [cardId, attachment.attachment_id])

  if (error) {
    return (
      <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--neon-red)' }}>
        {`> ${error}`}
      </div>
    )
  }
  const limit = expanded ? Infinity : PREVIEW_CHARS
  const truncated = text != null && text.length > limit
  return (
    <pre style={{
      margin: 0, padding: expanded ? '12px 14px' : '8px 10px',
      maxHeight: expanded ? '80vh' : 200, overflow: 'auto',
      background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
      borderRadius: 6, fontFamily: 'var(--font-mono)', fontSize: expanded ? 13 : 11,
      color: 'var(--text-primary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
      opacity: text == null ? 0.5 : 0.9,
    }}>
      {text == null ? 'loading…' : (truncated ? text.slice(0, limit) : text)}
      {truncated ? '\n… (truncated)' : ''}
    </pre>
  )
}
