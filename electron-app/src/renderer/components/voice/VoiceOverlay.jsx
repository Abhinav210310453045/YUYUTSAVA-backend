import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useConverse } from '../../hooks/useConverse'
import { audioPlayer } from '../../audio'
import ArtifactModal from '../artifacts/ArtifactModal'

// The mini voice overlay rendered in its own frameless transparent window.
// It opens (hotkey / wake word), plays an open earcon, listens, shows a compact
// transcript + reply, and auto-dismisses on a stop keyword or after idle —
// reusing the same useConverse WS conversation as the main Voice panel.

const BLUE = 'var(--text-info)'

// Spoken phrases that close the overlay ("Siri, that's all").
const STOP_KEYWORDS = ['thank you', 'thanks', 'ok then', 'okay then', 'stop', 'that\'s all', 'thats all', 'goodbye', 'bye']

// Auto-dismiss only after a long silence. The overlay stays attentive across
// turns so a follow-up question never needs a re-wake — any speech/reply/token
// activity resets this timer (see resetIdle below).
const IDLE_MS = 110000
// Exit-animation duration before the window is actually hidden.
const EXIT_MS = 240

function normalize(s) {
  return (s || '').toLowerCase().replace(/[.,!?;:]/g, ' ').replace(/\s+/g, ' ').trim()
}

function isStopPhrase(text) {
  const t = normalize(text)
  if (!t) return false
  return STOP_KEYWORDS.some((k) => t === k || t.startsWith(k + ' ') || t.endsWith(' ' + k) || t.includes(' ' + k + ' '))
}

// One accent per conversational state so the icon reads at a glance without
// looking at the label: dim while idle, bright cyan while listening, amber
// (sweeping ring) while thinking, soft blue (breathing) while speaking.
const STATE_ACCENT = {
  idle: '#8494c4',
  listening: '#7cd0ff',
  thinking: 'var(--neon-amber)',
  speaking: 'var(--text-info)',
}
const DISC_ANIM = {
  idle: 'voice-idle 3.4s ease-in-out infinite',
  listening: 'voice-beat 1.5s ease-in-out infinite, voice-aura 1.5s ease-out infinite',
  thinking: 'voice-idle 2.2s ease-in-out infinite',
  speaking: 'voice-speak 1.1s ease-in-out infinite',
}

function MicGlyph() {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="2" width="6" height="11" rx="3" />
      <path d="M5 10v1a7 7 0 0 0 14 0v-1" />
      <line x1="12" y1="18" x2="12" y2="22" />
      <line x1="8" y1="22" x2="16" y2="22" />
    </svg>
  )
}

// Live equaliser bars shown in place of the mic while the agent speaks.
function SpeakBars({ color }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 3, height: 22 }}>
      {[0, 1, 2, 3].map((i) => (
        <span key={i} style={{
          width: 3.5, height: '100%', borderRadius: 2, background: color,
          transformOrigin: 'center',
          animation: `voice-bar 0.9s ease-in-out ${i * 0.13}s infinite`,
        }} />
      ))}
    </div>
  )
}

function OverlayMic({ listening, speaking, busy }) {
  const state = speaking ? 'speaking' : busy ? 'thinking' : listening ? 'listening' : 'idle'
  const accent = STATE_ACCENT[state]
  return (
    <div style={{
      position: 'relative', width: 96, height: 96, flexShrink: 0,
      display: 'grid', placeItems: 'center',
    }}>
      {/* Sweeping conic ring — only rendered while thinking. A radial mask
          carves the filled disc into a thin ring so just the sweep shows. */}
      {state === 'thinking' && (
        <div style={{
          position: 'absolute', inset: 3, borderRadius: '50%',
          background: `conic-gradient(from 0deg, ${accent}00 0%, ${accent}00 60%, ${accent} 100%)`,
          WebkitMask: 'radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 3px))',
          mask: 'radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 3px))',
          animation: 'voice-think 0.9s linear infinite',
        }} />
      )}
      <div style={{
        width: 76, height: 76, borderRadius: '50%',
        display: 'grid', placeItems: 'center',
        background: `radial-gradient(circle at 50% 38%, ${accent}44, ${accent}0d)`,
        border: `1.5px solid ${accent}`, color: accent,
        animation: DISC_ANIM[state],
        transition: 'border-color 0.3s ease, color 0.3s ease',
      }}>
        {state === 'speaking' ? <SpeakBars color={accent} /> : <MicGlyph />}
      </div>
    </div>
  )
}

export default function VoiceOverlay() {
  const { messages, listening, speaking, busy, startVoice, stopVoice, interrupt } =
    useConverse({ origin: 'voice' })
  const [phase, setPhase] = useState('in') // 'in' | 'out'
  const [expandedArt, setExpandedArt] = useState(null) // artifact peeked big
  const idleTimer = useRef(null)
  const closingRef = useRef(false)

  // Latest user / assistant text for the compact two-line display.
  const lastUser = [...messages].reverse().find((m) => m.role === 'user')
  const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant')
  const artifacts = lastAssistant?.artifacts || []

  const dismiss = useCallback(() => {
    if (closingRef.current) return
    closingRef.current = true
    clearTimeout(idleTimer.current)
    setPhase('out')
    try { stopVoice() } catch { /* ignore */ }
    audioPlayer.stop()
    audioPlayer.playEarcon('close')
    setTimeout(() => { window.electronAPI?.closeOverlay?.() }, EXIT_MS)
  }, [stopVoice])

  const resetIdle = useCallback(() => {
    clearTimeout(idleTimer.current)
    idleTimer.current = setTimeout(dismiss, IDLE_MS)
  }, [dismiss])

  // (Re)activate each time main shows the overlay: play the open earcon, start
  // the live mic immediately (so "listening" is instant — the mic itself, via the
  // server VAD, decides what's speech vs. noise), and restart the idle timer. The
  // same-breath command is captured by this live mic, so there's nothing to wait
  // for.
  useEffect(() => {
    const activate = async () => {
      closingRef.current = false
      setPhase('in')
      try { await audioPlayer.ensureContext() } catch { /* ignore */ }
      audioPlayer.playEarcon('open')
      try { await startVoice() } catch { /* ignore */ }
      resetIdle()
    }
    // First mount counts as an activation.
    activate()
    const off = window.electronAPI?.onOverlayActivate?.(() => { activate() })
    return () => {
      off && off()
      clearTimeout(idleTimer.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Any conversational activity keeps the overlay alive.
  useEffect(() => { if (!closingRef.current) resetIdle() }, [messages, speaking, listening, resetIdle])

  // A spoken stop phrase closes the overlay.
  useEffect(() => {
    if (lastUser && isStopPhrase(lastUser.text)) dismiss()
  }, [lastUser, dismiss])

  // Esc closes it too.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') dismiss() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [dismiss])

  const state = speaking ? 'speaking' : busy ? 'thinking' : listening ? 'listening' : 'idle'
  const accent = STATE_ACCENT[state]
  const status = listening ? 'listening…' : speaking ? 'speaking…' : busy ? 'thinking…' : 'tap to talk'

  return (
    <div style={{
      height: '100vh', display: 'flex', alignItems: 'flex-end', justifyContent: 'center',
      padding: 12, boxSizing: 'border-box', background: 'transparent',
    }}>
      <div style={{
        width: '100%',
        display: 'flex', alignItems: 'flex-start', gap: 14,
        padding: '16px 18px',
        borderRadius: 18,
        background: 'rgba(12,14,22,0.82)',
        backdropFilter: 'blur(22px)',
        // Border + outer glow track the state accent so the whole pill shifts
        // hue with the mic (dim → cyan → amber → blue).
        border: `1px solid ${accent}55`,
        boxShadow: `0 8px 40px rgba(0,0,0,0.5), 0 0 26px ${accent}30`,
        transition: 'border-color 0.35s ease, box-shadow 0.35s ease',
        animation: `${phase === 'out' ? 'overlay-out' : 'overlay-in'} ${EXIT_MS}ms ease`,
        animationFillMode: 'both',
      }}>
        <button
          onClick={() => (listening ? stopVoice() : startVoice())}
          title={listening ? 'stop listening' : 'talk'}
          style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer' }}
        >
          <OverlayMic listening={listening} speaking={speaking} busy={busy} />
        </button>

        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
          <div style={{
            fontFamily: 'var(--font-mono)', fontSize: 10, letterSpacing: '0.12em',
            textTransform: 'uppercase', color: accent, transition: 'color 0.35s ease',
          }}>{status}</div>
          {/* Current exchange only (latest user line + latest assistant reply),
              rendered in full and streamed chunk-by-chunk. The reply scrolls
              within a bounded height for long answers rather than being clamped. */}
          <div style={{
            fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.4,
            wordBreak: 'break-word',
          }}>
            {lastUser ? `“${lastUser.text}”` : 'say something…'}
          </div>
          {lastAssistant && (
            <div style={{
              fontSize: 12, color: 'var(--text-primary)', lineHeight: 1.45,
              maxHeight: 180, overflowY: 'auto', wordBreak: 'break-word',
              whiteSpace: 'pre-wrap',
            }}>
              {lastAssistant.text}{lastAssistant.streaming ? <span style={{ color: BLUE }}>▋</span> : null}
            </div>
          )}
          {/* Artifacts made this turn — compact chips; tap to peek big. The
              full experience lives in the main chat window; this keeps the
              hands-free overlay from having to render a whole sandbox. */}
          {artifacts.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 4 }}>
              {artifacts.map((att) => (
                <button
                  key={att.attachment_id}
                  onClick={() => setExpandedArt(att)}
                  title="open artifact"
                  style={{
                    display: 'inline-flex', alignItems: 'center', gap: 5,
                    fontFamily: 'var(--font-mono)', fontSize: 10,
                    padding: '3px 9px', borderRadius: 8, cursor: 'pointer',
                    background: 'rgba(120,160,255,0.12)', color: 'var(--text-info)',
                    border: '1px solid rgba(120,160,255,0.3)',
                    maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}
                >🎨 {att.title || att.kind}</button>
              ))}
            </div>
          )}
        </div>

        <button
          onClick={() => { if (busy) interrupt(); dismiss() }}
          title="close (or say “thank you”)"
          style={{
            background: 'none', border: 'none', color: 'var(--text-muted)',
            cursor: 'pointer', fontSize: 18, lineHeight: 1, padding: 4, alignSelf: 'flex-start',
          }}
        >×</button>
      </div>

      <ArtifactModal attachment={expandedArt} onClose={() => setExpandedArt(null)} />
    </div>
  )
}
