import React, { useEffect, useState } from 'react'
import { todoAttachmentUrl } from '../../../api/client'

const TEXT_MIMES = ['text/plain', 'text/markdown', 'text/html', 'text/csv', 'application/json']
const PREVIEW_CHARS = 4000

export const matches = (att) =>
  att.kind === 'file' && TEXT_MIMES.includes(att.mime || '')

// Fetches the file's text and shows a scrollable plain-text preview (markdown
// and HTML render as source — never injected into the DOM).
export default function TextBlock({ attachment, cardId }) {
  const [text, setText] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    fetch(todoAttachmentUrl(cardId, attachment.attachment_id))
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
  const truncated = text != null && text.length > PREVIEW_CHARS
  return (
    <pre style={{
      margin: 0, padding: '8px 10px', maxHeight: 200, overflow: 'auto',
      background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
      borderRadius: 6, fontFamily: 'var(--font-mono)', fontSize: 11,
      color: 'var(--text-primary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
      opacity: text == null ? 0.5 : 0.9,
    }}>
      {text == null ? 'loading…' : text.slice(0, PREVIEW_CHARS)}
      {truncated ? '\n… (truncated)' : ''}
    </pre>
  )
}
