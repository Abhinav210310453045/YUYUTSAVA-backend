import React, { useState } from 'react'
import { submitFeedback } from '../../api/client'

// Hover actions under an assistant bubble: Copy, Regenerate, and 👍/👎 reactions.
// The reactions persist the (user, assistant) pair to the feedback table for a
// future feedback agent. Compact glass icon buttons with tap feedback.
//
// props:
//   message      — the assistant message ({ id, text, feedback? })
//   userText     — the preceding user turn's text (snapshotted with feedback)
//   sessionId    — hello.session_id (null → feedback disabled, e.g. brand-new)
//   onRegenerate — () => void  (re-send the preceding user text)
//   onFeedback   — (rating) => void  (persist selection on the message)

function IconBtn({ title, onClick, active, activeColor = 'var(--neon-green)', children }) {
  return (
    <button
      onClick={onClick} title={title} className="tap-pop"
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 4,
        fontFamily: 'var(--font-mono)', fontSize: 11, cursor: 'pointer',
        padding: '3px 7px', borderRadius: 7,
        background: active ? `${activeColor}1f` : 'var(--glass-bg)',
        border: `1px solid ${active ? activeColor : 'var(--glass-border)'}`,
        color: active ? activeColor : 'var(--text-muted)',
        backdropFilter: 'blur(8px)', transition: 'all 0.15s',
      }}
    >{children}</button>
  )
}

export default function MessageActions({ message, userText, sessionId, onRegenerate, onFeedback }) {
  const [copied, setCopied] = useState(false)
  const [note, setNote] = useState('')
  const [showNote, setShowNote] = useState(false)
  const rated = message.feedback

  const copy = async () => {
    try { await navigator.clipboard.writeText(message.text || ''); setCopied(true); setTimeout(() => setCopied(false), 1400) } catch { /* ignore */ }
  }

  const react = async (rating, extraNote = null) => {
    onFeedback?.(rating) // optimistic UI
    if (!sessionId) return
    try {
      await submitFeedback({
        session_id: sessionId,
        message_ref: message.id,
        rating,
        user_text: userText || '',
        assistant_text: message.text || '',
        note: extraNote,
      })
    } catch { /* non-fatal — the reaction still shows locally */ }
  }

  return (
    <div style={{ marginTop: 6 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        <IconBtn title="Copy reply" onClick={copy} active={copied}>
          {copied ? '✓ copied' : '⧉ copy'}
        </IconBtn>
        {onRegenerate && (
          <IconBtn title="Regenerate reply" onClick={onRegenerate}>↻ retry</IconBtn>
        )}
        <span style={{ flex: 1 }} />
        <IconBtn title="Good response" onClick={() => react('up')} active={rated === 'up'}>👍</IconBtn>
        <IconBtn
          title="Bad response — add a note" activeColor="var(--neon-red)"
          onClick={() => { react('down'); setShowNote((v) => !v) }}
          active={rated === 'down'}
        >👎</IconBtn>
      </div>
      {showNote && rated === 'down' && (
        <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
          <input
            value={note} onChange={(e) => setNote(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter' && note.trim()) { react('down', note.trim()); setShowNote(false) } }}
            placeholder="what was off? (optional, Enter to save)"
            style={{
              flex: 1, background: 'var(--bg-deep)', color: 'var(--text-primary)',
              border: '1px solid var(--border-card)', borderRadius: 6, padding: '5px 9px',
              fontSize: 11, fontFamily: 'var(--font-ui)',
            }}
          />
        </div>
      )}
    </div>
  )
}
