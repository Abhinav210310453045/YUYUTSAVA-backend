import React, { useState } from 'react'
import Markdown from './Markdown'
import MessageActions from './MessageActions'
import MessageArtifacts from './MessageArtifacts'
import MessageImages from './MessageImages'

// What a chat thread is made of: one bubble per message (with its tool rows,
// images, artifacts and actions), the empty state, and the jump-to-latest
// button. Pure rendering — the conversation lives in conversations/.

function ToolEvents({ events }) {
  if (!events || events.length === 0) return null
  return (
    <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 2 }}>
      {events.map((e, i) => {
        if (e.kind === 'tool_call') {
          const args = e.args ? JSON.stringify(e.args).slice(0, 100) : ''
          return (
            <div
              key={i}
              style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)' }}
            >
              · {e.name}
              {args ? ` ${args}` : ''}
            </div>
          )
        }
        if (e.kind === 'tool_result') {
          return (
            <div
              key={i}
              style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-dim)' }}
            >
              ↳ {e.name}: {(e.preview || '').slice(0, 80)}
            </div>
          )
        }
        return (
          <div
            key={i}
            style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--neon-amber)' }}
          >
            {e.text}
          </div>
        )
      })}
    </div>
  )
}

// Three-dot typing indicator shown while a reply streams in.
const TypingDots = () => (
  <span className="typing-dots" style={{ marginLeft: 2 }}>
    <i />
    <i />
    <i />
  </span>
)

// Glyph for the spoken-reply control: ▶ to play (or resume), ⏸ while audible.
function PlayPauseIcon({ playing }) {
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

// A standalone row for something the system needs to say — the model returned
// nothing, a history was repaired. Not a bubble: it is not something the agent
// said, and styling it as speech would misattribute it.
function NoticeRow({ m }) {
  const tone = {
    error: { fg: 'var(--neon-red)', bd: 'var(--border-red)', mark: '✗' },
    warning: { fg: 'var(--neon-amber)', bd: 'var(--border-amber)', mark: '⚠' },
    info: { fg: 'var(--text-muted)', bd: 'var(--border-subtle)', mark: '·' },
  }[m.level || 'info']
  return (
    <div
      style={{
        display: 'flex',
        gap: 8,
        alignItems: 'flex-start',
        flexShrink: 0,
        padding: '8px 12px',
        borderRadius: 'var(--radius-card)',
        border: `1px solid ${tone.bd}`,
        background: 'rgba(255,255,255,0.03)',
        fontSize: 12,
        lineHeight: 1.6,
        color: tone.fg,
      }}
    >
      <span style={{ fontFamily: 'var(--font-mono)' }}>{tone.mark}</span>
      <span style={{ color: 'var(--text-secondary)' }}>{m.text}</span>
    </div>
  )
}

export function Bubble({
  m,
  userText,
  sessionId,
  onRegenerate,
  onFeedback,
  playing,
  paused,
  onReplay,
  onTogglePause,
}) {
  // Hooks first: an early return above a hook changes the hook count between
  // renders, which React rejects if a bubble's role ever changes.
  const [hover, setHover] = useState(false)
  if (m.role === 'notice') return <NoticeRow m={m} />
  const isUser = m.role === 'user'
  const empty =
    !m.text && (!m.images || m.images.length === 0) && (!m.artifacts || m.artifacts.length === 0)
  // Spoken replies carry in-session PCM chunks (live turn) or a persisted
  // audio_url (resumed thread) — either makes the bubble playable.
  const hasAudio = !isUser && ((m.audioChunks && m.audioChunks.length > 0) || !!m.audioUrl)

  return (
    <div
      style={{ display: 'flex', justifyContent: isUser ? 'flex-end' : 'flex-start' }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <div
        className="hover-bulge"
        style={{
          maxWidth: '82%',
          background: isUser ? 'var(--grad-user)' : 'var(--glass-bg)',
          backdropFilter: 'blur(var(--glass-blur))',
          WebkitBackdropFilter: 'blur(var(--glass-blur))',
          border: `1px solid ${m.error ? 'var(--border-red)' : isUser ? 'transparent' : 'var(--glass-border)'}`,
          borderLeft: isUser ? undefined : m.error ? undefined : '2px solid transparent',
          borderImage: !isUser && !m.error ? 'var(--grad-accent) 1' : undefined,
          borderRadius: 16,
          padding: '10px 14px',
          color: m.error ? 'var(--neon-red)' : 'var(--text-primary)',
          fontSize: 13,
          lineHeight: 1.6,
          fontFamily: 'var(--font-ui)',
          wordBreak: 'break-word',
          animation: 'bubble-pop 0.28s cubic-bezier(0.34,1.56,0.64,1)',
          '--bulge-glow': isUser ? 'rgba(var(--accent-rgb),0.28)' : 'rgba(0,212,255,0.22)',
          boxShadow: 'var(--shadow-card)',
        }}
      >
        {isUser ? (
          <span style={{ whiteSpace: 'pre-wrap' }}>{m.text}</span>
        ) : (
          <>
            {/* Spoken-reply control: ▶ plays (or resumes a paused clip), ⏸
                pauses in place — the position holds, unlike the turn Stop. */}
            {hasAudio && (
              <button
                onClick={() => (playing ? onTogglePause() : onReplay(m))}
                title={
                  playing ? (paused ? 'resume playback' : 'pause playback') : 'play spoken reply'
                }
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
                  background:
                    playing && !paused ? 'rgba(120,160,255,0.30)' : 'rgba(120,160,255,0.12)',
                  border: `1px solid rgba(120,160,255,${playing && !paused ? 0.6 : 0.35})`,
                  color: 'var(--text-info)',
                  boxShadow: playing && !paused ? '0 0 10px rgba(120,160,255,0.5)' : 'none',
                  transition: 'background 0.2s, box-shadow 0.2s',
                }}
              >
                <PlayPauseIcon playing={playing && !paused} />
              </button>
            )}
            {empty && m.streaming ? <TypingDots /> : <Markdown>{m.text}</Markdown>}
            {m.text && m.streaming ? <TypingDots /> : null}
            <MessageImages images={m.images} />
            <MessageArtifacts artifacts={m.artifacts} />
            <ToolEvents events={m.events} />
            {!m.streaming && !m.error && !empty && (
              <div style={{ opacity: hover || m.feedback ? 1 : 0.55, transition: 'opacity 0.15s' }}>
                <MessageActions
                  message={m}
                  userText={userText}
                  sessionId={sessionId}
                  onRegenerate={onRegenerate}
                  onFeedback={onFeedback}
                />
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

// The one chat surface, reused (not forked) by every agent: the default props
// give the orchestrator panel; TodoCardView embeds it with agent='tinker' +
// card=<card_id> (thread pinned server-side to the card), its own header
// title/hints and no New button (the card IS the thread) — text and voice
// alike, since the mic streams over the same agent/card-pinned connection.
// `onTurnEnd` fires after each completed turn so a host view can refresh
// data the agent may have changed (e.g. notes added via todo_*).

export function EmptyThread({ glyph, hint }) {
  return (
    <div
      style={{
        flex: 1,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 10,
        color: 'var(--text-muted)',
        fontFamily: 'var(--font-mono)',
        fontSize: 12,
      }}
    >
      <div
        style={{
          fontSize: 40,
          fontWeight: 700,
          background: 'var(--grad-accent)',
          backgroundClip: 'text',
          WebkitBackgroundClip: 'text',
          WebkitTextFillColor: 'transparent',
        }}
        className="grad-animated"
      >
        {glyph}
      </div>
      <div>{hint}</div>
    </div>
  )
}

export function JumpToLatest({ onClick }) {
  return (
    <button
      onClick={onClick}
      style={{
        position: 'sticky',
        bottom: 4,
        alignSelf: 'center',
        zIndex: 3,
        padding: '3px 12px',
        borderRadius: 12,
        cursor: 'pointer',
        fontFamily: 'var(--font-mono)',
        fontSize: 10,
        background: 'var(--bg-elevated)',
        border: '1px solid var(--border-subtle)',
        color: 'var(--text-secondary)',
      }}
    >
      ↓ latest
    </button>
  )
}
