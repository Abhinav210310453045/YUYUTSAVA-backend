import React, { useEffect, useRef } from 'react'
import AskCard from './AskCard'
import { useStandaloneAsks } from '../../hooks/useAsks.jsx'
import { setAskShowing } from './overlayState'

// The surface that reaches you when you are not in YUYUTSAVA at all.
//
// An always-on-top, all-Spaces window that appears without stealing focus
// (`showInactive`), so a permission request finds you in whatever app you were
// actually using. It shows the same card as the owning view and the Inbox, so
// answering here is the same act — including the consent scopes.
//
// The X closes the window WITHOUT answering. That distinction is the whole
// point: dismissing a window is not a decision, so the ask stays pending in
// the Inbox and the agent keeps waiting. Nothing here ever auto-rejects.

export default function AskOverlay() {
  const { asks, answer, answering, dismiss } = useStandaloneAsks()
  // Asks hidden with the X this session. Local only — the record stays pending
  // on the daemon, and the Inbox still lists it.
  const hiddenRef = useRef(new Set())
  const shownRef = useRef(false)

  const visible = asks.filter((a) => !hiddenRef.current.has(a.ask_id))
  const ask = visible[0] || null

  // Tell the voice pill sharing this window to stand down while an ask is up.
  useEffect(() => {
    setAskShowing(!!ask)
    return () => setAskShowing(false)
  }, [ask])

  // Ask main to show/hide the overlay window as asks come and go. Main decides
  // whether the main window is focused (in which case the inline card and the
  // Inbox already have it covered) — the renderer just reports state.
  useEffect(() => {
    if (ask && !shownRef.current) {
      shownRef.current = true
      window.electronAPI?.showAskOverlay?.({
        ask_id: ask.ask_id,
        title: ask.title || '',
        agent: ask.agent_label || '',
      })
    } else if (!ask && shownRef.current) {
      shownRef.current = false
      window.electronAPI?.hideAskOverlay?.()
    }
  }, [ask])

  if (!ask) return null

  const onHide = () => {
    // Not an answer. The ask stays pending; only this window stops showing it.
    hiddenRef.current.add(ask.ask_id)
    dismiss(ask.ask_id)
    window.electronAPI?.hideAskOverlay?.()
    shownRef.current = false
  }

  return (
    <div style={{
      position: 'absolute',
      inset: 0,
      display: 'flex',
      flexDirection: 'column',
      justifyContent: 'flex-end',
      padding: 12,
      gap: 8,
      boxSizing: 'border-box',
      // The voice overlay shares this window; only the card itself may take
      // clicks, so an idle ask can't swallow taps meant for the mic.
      pointerEvents: 'none',
    }}>
      {/* Ambient wash so the card reads as lit rather than pasted on. */}
      <div aria-hidden style={{
        position: 'absolute', inset: 0, borderRadius: 22,
        background: 'radial-gradient(70% 55% at 50% 100%, rgba(251,191,36,0.10), transparent 70%)',
        pointerEvents: 'none',
      }} />

      {visible.length > 1 && (
        <div style={{
          alignSelf: 'flex-start',
          display: 'inline-flex', alignItems: 'center', gap: 6,
          fontFamily: 'var(--font-mono)', fontSize: 9, fontWeight: 'var(--fw-semibold)',
          letterSpacing: '0.1em', textTransform: 'uppercase',
          color: 'var(--text-muted)',
          background: 'rgba(255,255,255,0.05)',
          border: '1px solid var(--glass-border)',
          borderRadius: 999, padding: '4px 10px',
          backdropFilter: 'blur(10px)',
          position: 'relative',
        }}>
          +{visible.length - 1} more in your inbox
        </div>
      )}

      <div style={{ pointerEvents: 'auto', position: 'relative', animation: 'card-enter 0.24s ease' }}>
        <AskCard
          ask={ask}
          onAnswer={answer}
          answering={answering === ask.ask_id}
          dense
          headerAction={(
            <button
              onClick={onHide}
              title="Close without answering — it stays in your Inbox"
              className="tap-pop"
              style={{
                width: 22, height: 22, lineHeight: 1, borderRadius: '50%',
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid var(--glass-border)',
                color: 'var(--text-muted)', cursor: 'pointer',
                fontFamily: 'var(--font-mono)', fontSize: 11,
              }}
            >
              ✕
            </button>
          )}
        />
      </div>
    </div>
  )
}
