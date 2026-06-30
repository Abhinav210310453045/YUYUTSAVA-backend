import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useConverse } from '../../hooks/useConverse'
import { audioPlayer } from '../../audio'

// The mini voice overlay rendered in its own frameless transparent window.
// It opens (hotkey / wake word), plays an open earcon, listens, shows a compact
// transcript + reply, and auto-dismisses on a stop keyword or after idle —
// reusing the same useConverse WS conversation as the main Voice panel.

const BLUE = '#9bb8ff'

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

function OverlayMic({ listening, speaking }) {
  const animation = listening
    ? 'voice-beat 1.6s ease-in-out infinite, voice-aura 1.6s ease-out infinite'
    : 'voice-idle 3.2s ease-in-out infinite'
  return (
    <div style={{
      width: 84, height: 84, borderRadius: '50%', flexShrink: 0,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: listening
        ? 'radial-gradient(circle at 50% 40%, rgba(120,160,255,0.32), rgba(120,160,255,0.08))'
        : 'radial-gradient(circle at 50% 40%, rgba(120,160,255,0.14), rgba(120,160,255,0.03))',
      border: `1.5px solid rgba(120,160,255,${listening ? 0.6 : 0.3})`,
      color: BLUE, animation,
    }}>
      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <rect x="9" y="2" width="6" height="11" rx="3" />
        <path d="M5 10v1a7 7 0 0 0 14 0v-1" />
        <line x1="12" y1="18" x2="12" y2="22" />
        <line x1="8" y1="22" x2="16" y2="22" />
      </svg>
    </div>
  )
}

export default function VoiceOverlay() {
  const { messages, listening, speaking, busy, startVoice, stopVoice, interrupt } =
    useConverse({ origin: 'voice' })
  const [phase, setPhase] = useState('in') // 'in' | 'out'
  const idleTimer = useRef(null)
  const closingRef = useRef(false)

  // Latest user / assistant text for the compact two-line display.
  const lastUser = [...messages].reverse().find((m) => m.role === 'user')
  const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant')

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
        border: '1px solid rgba(120,160,255,0.28)',
        boxShadow: '0 8px 40px rgba(0,0,0,0.5), 0 0 24px rgba(120,160,255,0.18)',
        animation: `${phase === 'out' ? 'overlay-out' : 'overlay-in'} ${EXIT_MS}ms ease`,
        animationFillMode: 'both',
      }}>
        <button
          onClick={() => (listening ? stopVoice() : startVoice())}
          title={listening ? 'stop listening' : 'talk'}
          style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer' }}
        >
          <OverlayMic listening={listening} speaking={speaking} />
        </button>

        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
          <div style={{
            fontFamily: 'var(--font-mono)', fontSize: 10, letterSpacing: '0.12em',
            textTransform: 'uppercase', color: BLUE,
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
    </div>
  )
}
