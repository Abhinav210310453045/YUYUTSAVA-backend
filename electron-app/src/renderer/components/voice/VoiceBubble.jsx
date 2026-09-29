import React from 'react'
import Markdown from '../chat/Markdown'
import MessageImages from '../chat/MessageImages'
import NoticeRow from '../chat/NoticeRow'

const BLUE = 'var(--text-info)'

// One turn on the voice screen, with its ▶ replay toggle.

// Play / pause glyph for the per-message audio toggle. Two bars while a clip is
// audible (tap to stop), a triangle otherwise (tap to replay).
function PlayStopIcon({ playing }) {
  return playing ? (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <rect x="6" y="5" width="4" height="14" rx="1.2" />
      <rect x="14" y="5" width="4" height="14" rx="1.2" />
    </svg>
  ) : (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M8 5v14l11-7z" />
    </svg>
  )
}

export default function VoiceBubble({ m, playing, onReplay, noticeActions, onNoticeAction }) {
  // A notice is the system speaking, not the agent — the same row the chat
  // shows, not an agent bubble.
  if (m.role === 'notice') {
    return <NoticeRow m={m} actions={noticeActions} onAction={onNoticeAction} />
  }
  const isUser = m.role === 'user'
  // In-session turns carry raw PCM chunks; resumed turns carry a persisted
  // audio_url (Phase 6b) — both are replayable.
  const hasAudio = !isUser && ((m.audioChunks && m.audioChunks.length > 0) || !!m.audioUrl)
  return (
    <div style={{ display: 'flex', justifyContent: isUser ? 'flex-end' : 'flex-start' }}>
      <div
        className="hover-bulge"
        style={{
          maxWidth: '82%',
          background: isUser ? 'var(--grad-user)' : 'var(--glass-bg)',
          backdropFilter: 'blur(var(--glass-blur))',
          WebkitBackdropFilter: 'blur(var(--glass-blur))',
          border: `1px solid ${m.error ? 'var(--border-red)' : isUser ? 'rgba(120,160,255,0.28)' : 'var(--glass-border)'}`,
          borderRadius: 16,
          padding: '10px 14px',
          color: m.error ? 'var(--neon-red)' : 'var(--text-primary)',
          fontSize: 13,
          lineHeight: 1.6,
          fontFamily: 'var(--font-ui)',
          wordBreak: 'break-word',
          animation: 'bubble-pop 0.28s cubic-bezier(0.34,1.56,0.64,1)',
          '--bulge-glow': 'rgba(120,160,255,0.25)',
          boxShadow: 'var(--shadow-card)',
        }}
      >
        {hasAudio && (
          <button
            onClick={() => onReplay(m)}
            title={playing ? 'stop playback' : 'replay spoken reply'}
            className="tap-pop"
            style={{
              float: 'right',
              marginLeft: 8,
              cursor: 'pointer',
              width: 24,
              height: 24,
              borderRadius: '50%',
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              background: playing ? 'rgba(120,160,255,0.30)' : 'rgba(120,160,255,0.12)',
              border: `1px solid rgba(120,160,255,${playing ? 0.6 : 0.35})`,
              color: BLUE,
              boxShadow: playing ? '0 0 10px rgba(120,160,255,0.5)' : 'none',
              transition: 'background 0.2s, box-shadow 0.2s',
            }}
          >
            <PlayStopIcon playing={playing} />
          </button>
        )}
        {isUser ? (
          <span style={{ whiteSpace: 'pre-wrap' }}>{m.text}</span>
        ) : (
          <Markdown>{m.text}</Markdown>
        )}
        {m.streaming ? <span style={{ color: BLUE }}>▋</span> : null}
        <MessageImages images={m.images} />
      </div>
    </div>
  )
}
