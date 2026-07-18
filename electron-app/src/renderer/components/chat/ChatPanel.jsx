import React, { useEffect, useRef, useState } from 'react'
import { useConverse } from '../../hooks/useConverse'
import NewSessionButton from '../common/NewSessionButton'
import Markdown from './Markdown'
import MessageImages from './MessageImages'
import MessageArtifacts from './MessageArtifacts'
import MessageActions from './MessageActions'

function ToolEvents({ events }) {
  if (!events || events.length === 0) return null
  return (
    <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 2 }}>
      {events.map((e, i) => {
        if (e.kind === 'tool_call') {
          const args = e.args ? JSON.stringify(e.args).slice(0, 100) : ''
          return (
            <div key={i} style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-muted)' }}>
              · {e.name}{args ? ` ${args}` : ''}
            </div>
          )
        }
        if (e.kind === 'tool_result') {
          return (
            <div key={i} style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--text-dim)' }}>
              ↳ {e.name}: {(e.preview || '').slice(0, 80)}
            </div>
          )
        }
        return (
          <div key={i} style={{ fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--neon-amber)' }}>
            {e.text}
          </div>
        )
      })}
    </div>
  )
}

// Three-dot typing indicator shown while a reply streams in.
const TypingDots = () => (
  <span className="typing-dots" style={{ marginLeft: 2 }}><i /><i /><i /></span>
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

function Bubble({ m, userText, sessionId, onRegenerate, onFeedback, playing, paused, onReplay, onTogglePause }) {
  const isUser = m.role === 'user'
  const [hover, setHover] = useState(false)
  const empty = !m.text && (!m.images || m.images.length === 0) && (!m.artifacts || m.artifacts.length === 0)
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
          borderLeft: isUser ? undefined : (m.error ? undefined : '2px solid transparent'),
          borderImage: (!isUser && !m.error) ? 'var(--grad-accent) 1' : undefined,
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
                title={playing ? (paused ? 'resume playback' : 'pause playback') : 'play spoken reply'}
                className="tap-pop"
                style={{
                  float: 'right', marginLeft: 8, cursor: 'pointer',
                  width: 24, height: 24, borderRadius: '50%',
                  display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                  background: playing && !paused ? 'rgba(120,160,255,0.30)' : 'rgba(120,160,255,0.12)',
                  border: `1px solid rgba(120,160,255,${playing && !paused ? 0.6 : 0.35})`,
                  color: 'var(--text-info)',
                  boxShadow: playing && !paused ? '0 0 10px rgba(120,160,255,0.5)' : 'none',
                  transition: 'background 0.2s, box-shadow 0.2s',
                }}
              ><PlayPauseIcon playing={playing && !paused} /></button>
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
export default function ChatPanel({
  resumeId = null,
  active = true,
  agent = null,
  card = null,
  origin = 'ui',
  title = 'Chat — orchestrator',
  placeholder = 'message the orchestrator (Enter to send, Shift+Enter for newline)',
  emptyGlyph = '◈',
  emptyHint = '> talk to YUYUTSAVA — it can run tasks, make visuals, and delegate',
  showVoice = true,
  showNewSession = true,
  onTurnEnd = null,
  // { text, ts }: appends text into the composer draft. The ts nonce makes a
  // repeat selection retrigger; the user still reviews and hits send. Used by
  // the card view's "Ask Tinker about selection" handoff.
  draftSeed = null,
  // Selection-context chips (all optional — inert for Chat/Voice surfaces):
  // contextChips = [{ key, label, accent? }] rendered as removable pills above
  // the composer; the next send carries buildContext() invisibly and then
  // onChipsConsumed() fires so the host clears its selection.
  contextChips = null,
  onRemoveChip = null,
  onClearChips = null,
  buildContext = null,
  onChipsConsumed = null,
  // Extra controls the host renders on the right side of the header (e.g.
  // the card view's chat-history dropdown + New-chat button).
  headerActions = null,
  // Fires with the server hello ({ session_id, thread_id, resuming, … }) so a
  // host that drives resumeId can learn the id of a freshly minted session.
  onSessionChange = null,
}) {
  const {
    messages, connected, busy, pendingAsk, hello, listening, speaking, playingId, paused,
    send, answerAsk, interrupt, startVoice, stopVoice, replay, togglePause, newSession,
  } = useConverse({ origin, resumeId, agent, card })
  const [draft, setDraft] = useState('')
  const [askDraft, setAskDraft] = useState('')
  const [fb, setFb] = useState({}) // messageId -> 'up' | 'down' (local selection)
  const scrollRef = useRef(null)
  const wasBusyRef = useRef(false)

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, pendingAsk])

  // Notify the host view when a turn finishes (busy true → false).
  useEffect(() => {
    if (wasBusyRef.current && !busy) onTurnEnd?.()
    wasBusyRef.current = busy
  }, [busy, onTurnEnd])

  // Panel stays mounted when hidden — don't leave the push-to-talk mic hot.
  useEffect(() => {
    if (!active && listening) stopVoice()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  // Seed the composer from the host view (selection → Ask Tinker). Appends
  // rather than replaces so an in-progress draft is never clobbered.
  useEffect(() => {
    if (draftSeed?.text) setDraft((d) => (d ? `${d}\n${draftSeed.text}` : draftSeed.text))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftSeed?.ts])

  // Surface the live session id to the host once the server hello lands.
  useEffect(() => {
    if (hello?.session_id) onSessionChange?.(hello)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hello?.session_id])

  const onSubmit = () => {
    const hasChips = contextChips && contextChips.length > 0
    const ok = send(draft, {
      context: hasChips && buildContext ? buildContext() : undefined,
    })
    if (!ok) return // keep draft + chips — the frame never left (disconnected/busy)
    setDraft('')
    if (hasChips) onChipsConsumed?.()
  }
  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSubmit() }
  }

  // Text of the user turn immediately preceding message index i (for feedback
  // snapshot + regenerate).
  const precedingUserText = (i) => {
    for (let j = i - 1; j >= 0; j--) if (messages[j].role === 'user') return messages[j].text
    return ''
  }

  const askBody = pendingAsk?.payload?.body || pendingAsk?.payload?.question
    || pendingAsk?.payload?.reason || pendingAsk?.payload?.text || 'The agent is asking for input.'
  const askTitle = pendingAsk?.payload?.title
    || (pendingAsk?.payload?.type === 'task_runner_permission' ? 'Permission requested' : 'Question')

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', position: 'relative' }}>
      {/* header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '14px 24px', borderBottom: '1px solid var(--border-subtle)', background: 'var(--bg-bar)',
      }}>
        <span style={{
          width: 8, height: 8, borderRadius: '50%',
          background: connected ? 'var(--neon-green)' : 'var(--neon-red)',
          boxShadow: connected ? '0 0 6px var(--neon-green)' : 'none',
        }} />
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 11, letterSpacing: '0.1em',
          textTransform: 'uppercase', color: 'var(--text-primary)', fontWeight: 'var(--fw-semibold)',
        }}>{title}</span>
        {(headerActions || showNewSession) && (
          <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            {headerActions}
            {showNewSession && <NewSessionButton onClick={newSession} label="New chat" />}
          </span>
        )}
      </div>

      {/* animated gradient mesh behind the thread */}
      <div aria-hidden style={{
        position: 'absolute', inset: 0, top: 50, background: 'var(--grad-mesh)',
        opacity: 0.7, pointerEvents: 'none', animation: 'mesh-drift 18s ease-in-out infinite',
        zIndex: 0,
      }} />

      {/* messages */}
      <div ref={scrollRef} style={{
        flex: 1, overflowY: 'auto', padding: '20px 24px', position: 'relative', zIndex: 1,
        display: 'flex', flexDirection: 'column', gap: 12,
      }}>
        {messages.length === 0 && (
          <div style={{
            flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', gap: 10, color: 'var(--text-muted)',
            fontFamily: 'var(--font-mono)', fontSize: 12,
          }}>
            <div style={{
              fontSize: 40, fontWeight: 700,
              background: 'var(--grad-accent)', backgroundClip: 'text', WebkitBackgroundClip: 'text',
              WebkitTextFillColor: 'transparent',
            }} className="grad-animated">{emptyGlyph}</div>
            <div>{emptyHint}</div>
          </div>
        )}
        {messages.map((m, i) => (
          <Bubble
            key={m.id}
            m={{ ...m, feedback: fb[m.id] }}
            userText={precedingUserText(i)}
            sessionId={hello?.session_id || null}
            onRegenerate={m.role === 'assistant' && !busy ? () => send(precedingUserText(i)) : null}
            onFeedback={(rating) => setFb((p) => ({ ...p, [m.id]: rating }))}
            playing={playingId === m.id}
            paused={playingId === m.id && paused}
            onReplay={replay}
            onTogglePause={togglePause}
          />
        ))}

        {pendingAsk && (
          <div style={{
            border: '1px solid var(--neon-amber)', borderRadius: 'var(--radius-card)',
            padding: '12px 14px', background: 'rgba(255,176,0,0.06)', backdropFilter: 'blur(8px)',
          }}>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--neon-amber)', marginBottom: 6 }}>
              ▣ {askTitle}
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-primary)', marginBottom: 8 }}>{askBody}</div>
            <div style={{ display: 'flex', gap: 8 }}>
              <input
                value={askDraft}
                onChange={(e) => setAskDraft(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && askDraft.trim()) { answerAsk(askDraft.trim()); setAskDraft('') } }}
                placeholder="type a reply, or use the buttons"
                style={{
                  flex: 1, background: 'var(--bg-deep)', color: 'var(--text-primary)',
                  border: '1px solid var(--border-card)', borderRadius: 6, padding: '6px 10px', fontSize: 12,
                }}
              />
              <button onClick={() => answerAsk('yes')} style={btnStyle(true)}>approve</button>
              <button onClick={() => answerAsk('no')} style={btnStyle(false)}>reject</button>
            </div>
          </div>
        )}
      </div>

      {/* selection-context chips — what the next message will be scoped to */}
      {contextChips && contextChips.length > 0 && (
        <div style={{
          display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6,
          padding: '8px 24px 0', position: 'relative', zIndex: 1,
        }}>
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>
            context
          </span>
          {contextChips.map((chip) => (
            <span
              key={chip.key}
              title={chip.title || chip.label}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 5,
                fontFamily: 'var(--font-mono)', fontSize: 10,
                padding: '2px 8px', borderRadius: 10,
                background: chip.accent?.glow || 'rgba(120,160,255,0.10)',
                color: chip.accent?.bar || 'var(--text-info)',
                border: `1px solid ${chip.accent?.border || 'rgba(120,160,255,0.3)'}`,
                maxWidth: 220,
              }}
            >
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {chip.label}
              </span>
              <button
                onClick={() => onRemoveChip?.(chip.key)}
                title="remove from context"
                style={{
                  background: 'none', border: 'none', color: 'inherit',
                  cursor: 'pointer', padding: 0, fontSize: 10, lineHeight: 1,
                }}
              >
                ✕
              </button>
            </span>
          ))}
          {contextChips.length > 1 && (
            <button
              onClick={() => onClearChips?.()}
              style={{
                fontFamily: 'var(--font-mono)', fontSize: 9, padding: '2px 8px',
                background: 'transparent', color: 'var(--text-muted)',
                border: '1px solid var(--border-card)', borderRadius: 10, cursor: 'pointer',
              }}
            >
              clear all
            </button>
          )}
        </div>
      )}

      {/* composer */}
      <div style={{
        display: 'flex', gap: 8, padding: '12px 24px 18px', position: 'relative', zIndex: 1,
        borderTop: '1px solid var(--border-subtle)',
      }}>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          rows={1}
          placeholder={busy ? 'agent is working…' : placeholder}
          style={{
            flex: 1, resize: 'none', background: 'var(--glass-bg)', color: 'var(--text-primary)',
            border: '1px solid var(--glass-border)', borderRadius: 22, padding: '11px 16px',
            fontSize: 13, fontFamily: 'var(--font-ui)', maxHeight: 120,
            backdropFilter: 'blur(var(--glass-blur))', outline: 'none',
          }}
        />
        {showVoice && (
          <button
            onClick={() => (listening ? stopVoice() : startVoice())}
            title={listening ? 'stop microphone' : 'talk to the agent'}
            className="tap-pop"
            style={micBtnStyle(listening)}
          >
            {listening ? '● mic' : '🎙 mic'}
          </button>
        )}
        {busy ? (
          <button onClick={interrupt} className="tap-pop" style={btnStyle(false)}>stop</button>
        ) : (
          <button onClick={onSubmit} disabled={!draft.trim()} className="grad-animated tap-pop" style={sendStyle(!!draft.trim())}>send</button>
        )}
      </div>
      {(listening || speaking) && (
        <div style={{
          padding: '0 24px 10px', fontFamily: 'var(--font-mono)', fontSize: 10, position: 'relative', zIndex: 1,
          color: speaking ? 'var(--neon-green)' : 'var(--neon-amber)',
        }}>
          {speaking ? '▸ agent speaking…' : '● listening — speak, then pause (or stop the mic)'}
        </div>
      )}
    </div>
  )
}

function btnStyle(primary) {
  return {
    fontFamily: 'var(--font-mono)', fontSize: 12, cursor: 'pointer',
    padding: '8px 14px', borderRadius: 10,
    background: primary ? 'rgba(var(--accent-rgb),0.1)' : 'rgba(255,51,102,0.08)',
    border: `1px solid ${primary ? 'rgba(var(--accent-rgb),0.3)' : 'rgba(255,51,102,0.3)'}`,
    color: primary ? 'var(--neon-green)' : 'var(--neon-red)',
  }
}

function sendStyle(enabled) {
  return {
    fontFamily: 'var(--font-mono)', fontSize: 12, fontWeight: 700, cursor: enabled ? 'pointer' : 'default',
    padding: '8px 18px', borderRadius: 22, border: 'none',
    background: enabled ? 'var(--grad-accent)' : 'var(--glass-bg)',
    color: enabled ? '#04120a' : 'var(--text-dim)',
    opacity: enabled ? 1 : 0.6,
    boxShadow: enabled ? '0 2px 14px rgba(var(--accent-rgb),0.3)' : 'none',
  }
}

function micBtnStyle(active) {
  return {
    fontFamily: 'var(--font-mono)', fontSize: 12, cursor: 'pointer',
    padding: '8px 12px', borderRadius: 22,
    background: active ? 'rgba(120,160,255,0.18)' : 'rgba(120,160,255,0.06)',
    border: `1px solid rgba(120,160,255,${active ? 0.5 : 0.3})`,
    color: 'var(--text-info)',
  }
}
