import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useConverse } from '../../hooks/useConverse'
import { useSettings } from '../../hooks/useSettings'
import WakeWordOnboarding from './WakeWordOnboarding'
import NewSessionButton from '../common/NewSessionButton'
import VoiceOrb from './VoiceOrb'
import Markdown from '../chat/Markdown'
import MessageImages from '../chat/MessageImages'

// Persisted flag (in the daemon .env via main/settings.js) for the dismissible
// "wake keywords live in Settings" note. Stored as a UI-only env key.
const NOTE_KEY = 'UI_VOICE_NOTE_DISMISSED'

const BLUE = 'var(--text-info)'

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

const MIC_POS_KEY = 'yuyutsava.voice.micPos'

function loadMicPos() {
  try {
    const v = JSON.parse(localStorage.getItem(MIC_POS_KEY))
    if (v && typeof v.x === 'number' && typeof v.y === 'number') return v
  } catch { /* ignore */ }
  return null
}

// The centerpiece, now a free-floating puck you can drag anywhere in the panel
// (both axes) so it never sits on top of a chart/answer — tap it to talk, drag
// it out of the way. The reactive orb (canvas, amplitude-driven) blooms behind a
// mic glyph; the status pill + stop travel with it. Position persists per device.
function FloatingMic({ boundsRef, listening, speaking, busy, getMicAnalyser, status, onToggle, onStop }) {
  const ORB = 168
  const PILL_RESERVE = 54
  const [pos, setPos] = useState(loadMicPos)
  const posRef = useRef(pos)
  posRef.current = pos
  const drag = useRef(null)

  const clampToBounds = useCallback((p) => {
    const b = boundsRef.current?.getBoundingClientRect()
    if (!b) return p
    const maxX = Math.max(0, b.width - ORB)
    const maxY = Math.max(0, b.height - ORB - PILL_RESERVE)
    return { x: Math.min(Math.max(0, p.x), maxX), y: Math.min(Math.max(0, p.y), maxY) }
  }, [boundsRef])

  // Seed once the panel actually has a size (default: bottom-right, clear of the
  // left-aligned answer/chart bubbles) and re-clamp on every resize. A
  // ResizeObserver — rather than a one-shot measure — covers the case where the
  // panel mounts hidden (0×0) and only gets real bounds when you navigate to it.
  // NOTE: a passive effect (not layout) — a child's layout effect runs before
  // the *parent's* ref (boundsRef) is attached on first mount, so it'd read null.
  useEffect(() => {
    const el = boundsRef.current
    if (!el) return
    const measure = () => {
      const b = el.getBoundingClientRect()
      if (b.width < 40 || b.height < 40) return // hidden / not laid out yet
      setPos((cur) => cur
        ? clampToBounds(cur)
        : { x: Math.max(0, b.width - ORB - 10), y: Math.max(0, b.height - ORB - PILL_RESERVE - 6) })
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onPointerMove = useCallback((e) => {
    const d = drag.current
    if (!d) return
    const dx = e.clientX - d.sx, dy = e.clientY - d.sy
    if (!d.moved && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) d.moved = true
    if (d.moved) setPos(clampToBounds({ x: d.ox + dx, y: d.oy + dy }))
  }, [clampToBounds])

  const onPointerUp = useCallback(() => {
    window.removeEventListener('pointermove', onPointerMove)
    window.removeEventListener('pointerup', onPointerUp)
    const d = drag.current
    drag.current = null
    if (!d) return
    if (!d.moved) { onToggle(); return } // a clean tap = toggle the mic
    try { localStorage.setItem(MIC_POS_KEY, JSON.stringify(posRef.current)) } catch { /* ignore */ }
    setPos((p) => ({ ...p })) // nudge a re-render to drop the grabbing cursor
  }, [onPointerMove, onToggle])

  const onPointerDown = (e) => {
    if (e.button != null && e.button !== 0) return
    e.preventDefault()
    drag.current = { sx: e.clientX, sy: e.clientY, ox: posRef.current?.x ?? 0, oy: posRef.current?.y ?? 0, moved: false }
    window.addEventListener('pointermove', onPointerMove)
    window.addEventListener('pointerup', onPointerUp)
  }

  if (!pos) return null
  const grabbing = !!drag.current
  return (
    <div style={{
      position: 'absolute', left: pos.x, top: pos.y, zIndex: 5, width: ORB,
      display: 'flex', flexDirection: 'column', alignItems: 'center', pointerEvents: 'none',
    }}>
      {/* orb + glyph = the drag handle (a clean tap toggles the mic) */}
      <div
        onPointerDown={onPointerDown}
        title={listening ? 'drag to move · tap to stop' : 'drag to move · tap to talk'}
        style={{
          position: 'relative', width: ORB, height: ORB, pointerEvents: 'auto',
          cursor: grabbing ? 'grabbing' : 'grab', touchAction: 'none',
        }}
      >
        <div style={{ position: 'absolute', inset: 0 }}>
          <VoiceOrb listening={listening} speaking={speaking} getMicAnalyser={getMicAnalyser} size={ORB} />
        </div>
        <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div style={{
            width: 70, height: 70, borderRadius: '50%',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            background: listening
              ? 'radial-gradient(circle at 50% 34%, rgba(120,160,255,0.30), rgba(10,14,22,0.5))'
              : 'radial-gradient(circle at 50% 34%, rgba(120,160,255,0.14), rgba(10,14,22,0.46))',
            border: `1.5px solid rgba(120,160,255,${listening ? 0.65 : 0.32})`,
            backdropFilter: 'blur(4px)', WebkitBackdropFilter: 'blur(4px)',
            color: BLUE, transition: 'border-color 0.3s, background 0.3s',
            boxShadow: listening ? '0 0 24px rgba(120,160,255,0.4)' : '0 0 14px rgba(120,160,255,0.16)',
          }}>
            <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <rect x="9" y="2" width="6" height="11" rx="3" />
              <path d="M5 10v1a7 7 0 0 0 14 0v-1" />
              <line x1="12" y1="18" x2="12" y2="22" />
              <line x1="8" y1="22" x2="16" y2="22" />
            </svg>
          </div>
        </div>
      </div>
      {/* status pill + stop — travel with the mic */}
      <div style={{ marginTop: -4, display: 'flex', alignItems: 'center', gap: 8, pointerEvents: 'auto' }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8, padding: '5px 12px', borderRadius: 999,
          background: 'var(--glass-bg)', border: '1px solid var(--glass-border)',
          backdropFilter: 'blur(var(--glass-blur))', WebkitBackdropFilter: 'blur(var(--glass-blur))',
          fontFamily: 'var(--font-mono)', fontSize: 10, whiteSpace: 'nowrap',
          color: (listening || speaking) ? BLUE : 'var(--text-muted)',
        }}>
          {speaking && <SpeakingBars />}
          <span>{status}</span>
        </div>
        {busy && (
          <button onClick={onStop} className="tap-pop" style={{
            fontFamily: 'var(--font-mono)', fontSize: 10, cursor: 'pointer',
            padding: '5px 12px', borderRadius: 999,
            background: 'rgba(255,51,102,0.1)', border: '1px solid rgba(255,51,102,0.3)',
            color: 'var(--neon-red)',
          }}>stop</button>
        )}
      </div>
    </div>
  )
}

// Animated equaliser shown while the agent is speaking.
function SpeakingBars() {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 3, height: 12 }}>
      {[0, 1, 2, 3, 4].map((i) => (
        <span key={i} style={{
          width: 3, height: 12, background: BLUE, borderRadius: 2,
          transformOrigin: 'bottom',
          animation: `voice-bar 0.9s ease-in-out ${i * 0.12}s infinite`,
        }} />
      ))}
    </div>
  )
}

function VoiceBubble({ m, playing, onReplay }) {
  const isUser = m.role === 'user'
  // In-session turns carry raw PCM chunks; resumed turns carry a persisted
  // audio_url (Phase 6b) — both are replayable.
  const hasAudio = !isUser && ((m.audioChunks && m.audioChunks.length > 0) || !!m.audioUrl)
  return (
    <div style={{ display: 'flex', justifyContent: isUser ? 'flex-end' : 'flex-start' }}>
      <div className="hover-bulge" style={{
        maxWidth: '82%',
        background: isUser ? 'var(--grad-user)' : 'var(--glass-bg)',
        backdropFilter: 'blur(var(--glass-blur))',
        WebkitBackdropFilter: 'blur(var(--glass-blur))',
        border: `1px solid ${m.error ? 'var(--border-red)' : isUser ? 'rgba(120,160,255,0.28)' : 'var(--glass-border)'}`,
        borderRadius: 16,
        padding: '10px 14px',
        color: m.error ? 'var(--neon-red)' : 'var(--text-primary)',
        fontSize: 13, lineHeight: 1.6, fontFamily: 'var(--font-ui)',
        wordBreak: 'break-word',
        animation: 'bubble-pop 0.28s cubic-bezier(0.34,1.56,0.64,1)',
        '--bulge-glow': 'rgba(120,160,255,0.25)',
        boxShadow: 'var(--shadow-card)',
      }}>
        {hasAudio && (
          <button
            onClick={() => onReplay(m)}
            title={playing ? 'stop playback' : 'replay spoken reply'}
            className="tap-pop"
            style={{
              float: 'right', marginLeft: 8, cursor: 'pointer',
              width: 24, height: 24, borderRadius: '50%',
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
              background: playing ? 'rgba(120,160,255,0.30)' : 'rgba(120,160,255,0.12)',
              border: `1px solid rgba(120,160,255,${playing ? 0.6 : 0.35})`,
              color: BLUE,
              boxShadow: playing ? '0 0 10px rgba(120,160,255,0.5)' : 'none',
              transition: 'background 0.2s, box-shadow 0.2s',
            }}
          ><PlayStopIcon playing={playing} /></button>
        )}
        {isUser
          ? <span style={{ whiteSpace: 'pre-wrap' }}>{m.text}</span>
          : <Markdown>{m.text}</Markdown>}
        {m.streaming ? <span style={{ color: BLUE }}>▋</span> : null}
        <MessageImages images={m.images} />
      </div>
    </div>
  )
}

export default function VoicePanel({ onOpenSettings, autoStartSignal = 0, resumeId = null, active = true }) {
  const {
    messages, connected, busy, pendingAsk, listening, speaking, playingId,
    answerAsk, startVoice, stopVoice, interrupt, replay, newSession, getMicAnalyser,
  } = useConverse({ origin: 'voice', resumeId })
  const { settings, loading: settingsLoading, save } = useSettings()
  const [askDraft, setAskDraft] = useState('')
  const scrollRef = useRef(null)
  const panelRef = useRef(null)

  const noteDismissed = settings[NOTE_KEY] === '1'
  // First-run wake-word setup: no wake word chosen yet and not yet onboarded.
  const needsOnboarding = !settingsLoading
    && !settings.WAKE_WORDS
    && settings.UI_WAKE_ONBOARDED !== '1'

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, pendingAsk])

  // Hotkey/wake while the window is focused bumps autoStartSignal → start the
  // mic. Skip the initial mount (signal 0) so opening the panel by hand is quiet.
  useEffect(() => {
    if (autoStartSignal > 0 && !listening) startVoice()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoStartSignal])

  // The panel now stays mounted when you navigate away (so the conversation
  // survives). Stop the mic when it's hidden so we never leave a hot mic running
  // in the background — and so it can't collide with the wake-word overlay's mic.
  useEffect(() => {
    if (!active && listening) stopVoice()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])

  const dismissNote = () => { save({ [NOTE_KEY]: '1' }) }

  // Kept short — the pill floats with the draggable mic, so a terse label reads
  // cleaner than a full sentence.
  const status = speaking
    ? 'speaking…'
    : busy ? 'working…'
    : listening ? 'listening…'
    : 'tap to talk · drag to move'

  const askBody = pendingAsk?.payload?.body || pendingAsk?.payload?.question
    || pendingAsk?.payload?.reason || pendingAsk?.payload?.text || 'The agent is asking for input.'

  return (
    <div ref={panelRef} style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', position: 'relative' }}>
      {needsOnboarding && (
        <WakeWordOnboarding save={save} onDone={() => {}} />
      )}

      {/* drifting gradient mesh — one continuous surface behind the thread */}
      <div aria-hidden style={{
        position: 'absolute', inset: 0, background: 'var(--grad-mesh)',
        opacity: 0.7, pointerEvents: 'none', animation: 'mesh-drift 18s ease-in-out infinite', zIndex: 0,
      }} />

      {/* header — borderless, floats over the mesh */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '14px 24px', position: 'relative', zIndex: 2,
      }}>
        <span style={{
          width: 8, height: 8, borderRadius: '50%',
          background: connected ? BLUE : 'var(--neon-red)',
          boxShadow: connected ? `0 0 6px ${BLUE}` : 'none',
        }} />
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 11, letterSpacing: '0.1em',
          textTransform: 'uppercase', fontWeight: 700, flex: 1,
          background: 'linear-gradient(90deg, #cdd9ff, var(--text-info) 60%, #8b5cf6)',
          WebkitBackgroundClip: 'text', backgroundClip: 'text', WebkitTextFillColor: 'transparent',
        }}>Voice — talk to YUYUTSAVA</span>
        <NewSessionButton onClick={() => { stopVoice(); newSession() }} label="New" color={BLUE} />
      </div>

      {/* dismissible wake-word note */}
      {!noteDismissed && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10, position: 'relative', zIndex: 2,
          margin: '4px 24px 0', padding: '8px 12px',
          background: 'rgba(120,160,255,0.06)',
          border: '1px solid rgba(120,160,255,0.22)', borderRadius: 12,
          fontSize: 12, color: 'var(--text-secondary)',
        }}>
          <span style={{ color: BLUE }}>✦</span>
          <span style={{ flex: 1 }}>
            You can add or remove wake keywords in{' '}
            <button onClick={onOpenSettings} style={{
              background: 'none', border: 'none', color: BLUE, cursor: 'pointer',
              padding: 0, font: 'inherit', textDecoration: 'underline',
            }}>Settings</button>.
          </span>
          <button onClick={dismissNote} title="dismiss" style={{
            background: 'none', border: 'none', color: 'var(--text-muted)',
            cursor: 'pointer', fontSize: 16, lineHeight: 1, padding: 0,
          }}>×</button>
        </div>
      )}

      {/* transcript / conversation thread — fills the panel; the mic floats over
          it (draggable) so nothing is permanently reserved or covered */}
      <div ref={scrollRef} style={{
        flex: 1, overflowY: 'auto', padding: '14px 24px 100px', position: 'relative', zIndex: 1,
        display: 'flex', flexDirection: 'column', gap: 12,
      }}>
        {messages.length === 0 && (
          <div style={{
            flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', gap: 10, color: 'var(--text-muted)',
            fontFamily: 'var(--font-mono)', fontSize: 12, textAlign: 'center',
          }}>
            <div className="grad-animated" style={{
              fontSize: 34, fontWeight: 700,
              background: 'var(--grad-accent)', WebkitBackgroundClip: 'text', backgroundClip: 'text',
              WebkitTextFillColor: 'transparent',
            }}>◍</div>
            <div style={{ maxWidth: 340, lineHeight: 1.6 }}>
              say something — your words and YUYUTSAVA&apos;s replies land here
            </div>
          </div>
        )}
        {messages.map((m) => (
          <VoiceBubble key={m.id} m={m} playing={playingId === m.id} onReplay={replay} />
        ))}

        {pendingAsk && (
          <div style={{
            border: '1px solid var(--neon-amber)', borderRadius: 14,
            padding: '12px 14px', background: 'rgba(255,176,0,0.06)', backdropFilter: 'blur(8px)',
          }}>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--neon-amber)', marginBottom: 6 }}>
              ▣ Question
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
                  border: '1px solid var(--border-card)', borderRadius: 8, padding: '6px 10px', fontSize: 12,
                }}
              />
              <button onClick={() => answerAsk('yes')} style={askBtn(true)}>approve</button>
              <button onClick={() => answerAsk('no')} style={askBtn(false)}>reject</button>
            </div>
          </div>
        )}
      </div>

      {/* draggable floating mic — lives over the thread, movable anywhere so it
          never blocks a chart/answer */}
      <FloatingMic
        boundsRef={panelRef}
        listening={listening}
        speaking={speaking}
        busy={busy}
        getMicAnalyser={getMicAnalyser}
        status={status}
        onToggle={() => (listening ? stopVoice() : startVoice())}
        onStop={interrupt}
      />
    </div>
  )
}

function askBtn(primary) {
  return {
    fontFamily: 'var(--font-mono)', fontSize: 12, cursor: 'pointer',
    padding: '6px 12px', borderRadius: 10,
    background: primary ? 'rgba(var(--accent-rgb),0.1)' : 'rgba(255,51,102,0.08)',
    border: `1px solid ${primary ? 'rgba(var(--accent-rgb),0.3)' : 'rgba(255,51,102,0.3)'}`,
    color: primary ? 'var(--neon-green)' : 'var(--neon-red)',
  }
}
