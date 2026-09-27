import React, { useCallback, useEffect, useRef, useState } from 'react'
import VoiceOrb from './VoiceOrb'

const BLUE = 'var(--text-info)'

// The voice screen's draggable mic: the orb, its status pill, the speaking
// bars, and where the user last parked it.

const MIC_POS_KEY = 'yuyutsava.voice.micPos'

function loadMicPos() {
  try {
    const v = JSON.parse(localStorage.getItem(MIC_POS_KEY))
    if (v && typeof v.x === 'number' && typeof v.y === 'number') return v
  } catch {
    /* ignore */
  }
  return null
}

// The centerpiece, now a free-floating puck you can drag anywhere in the panel
// (both axes) so it never sits on top of a chart/answer — tap it to talk, drag
// it out of the way. The reactive orb (canvas, amplitude-driven) blooms behind a
// mic glyph; the status pill + stop travel with it. Position persists per device.
export default function FloatingMic({
  boundsRef,
  listening,
  speaking,
  busy,
  getMicAnalyser,
  status,
  onToggle,
  onStop,
}) {
  const ORB = 168
  const PILL_RESERVE = 54
  const [pos, setPos] = useState(loadMicPos)
  const posRef = useRef(pos)
  posRef.current = pos
  const drag = useRef(null)

  const clampToBounds = useCallback(
    (p) => {
      const b = boundsRef.current?.getBoundingClientRect()
      if (!b) return p
      const maxX = Math.max(0, b.width - ORB)
      const maxY = Math.max(0, b.height - ORB - PILL_RESERVE)
      return { x: Math.min(Math.max(0, p.x), maxX), y: Math.min(Math.max(0, p.y), maxY) }
    },
    [boundsRef],
  )

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
      setPos((cur) =>
        cur
          ? clampToBounds(cur)
          : {
              x: Math.max(0, b.width - ORB - 10),
              y: Math.max(0, b.height - ORB - PILL_RESERVE - 6),
            },
      )
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const onPointerMove = useCallback(
    (e) => {
      const d = drag.current
      if (!d) return
      const dx = e.clientX - d.sx,
        dy = e.clientY - d.sy
      if (!d.moved && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) d.moved = true
      if (d.moved) setPos(clampToBounds({ x: d.ox + dx, y: d.oy + dy }))
    },
    [clampToBounds],
  )

  const onPointerUp = useCallback(() => {
    window.removeEventListener('pointermove', onPointerMove)
    window.removeEventListener('pointerup', onPointerUp)
    const d = drag.current
    drag.current = null
    if (!d) return
    if (!d.moved) {
      onToggle()
      return
    } // a clean tap = toggle the mic
    try {
      localStorage.setItem(MIC_POS_KEY, JSON.stringify(posRef.current))
    } catch {
      /* ignore */
    }
    setPos((p) => ({ ...p })) // nudge a re-render to drop the grabbing cursor
  }, [onPointerMove, onToggle])

  const onPointerDown = (e) => {
    if (e.button != null && e.button !== 0) return
    e.preventDefault()
    drag.current = {
      sx: e.clientX,
      sy: e.clientY,
      ox: posRef.current?.x ?? 0,
      oy: posRef.current?.y ?? 0,
      moved: false,
    }
    window.addEventListener('pointermove', onPointerMove)
    window.addEventListener('pointerup', onPointerUp)
  }

  if (!pos) return null
  const grabbing = !!drag.current
  return (
    <div
      style={{
        position: 'absolute',
        left: pos.x,
        top: pos.y,
        zIndex: 5,
        width: ORB,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        pointerEvents: 'none',
      }}
    >
      {/* orb + glyph = the drag handle (a clean tap toggles the mic) */}
      <div
        onPointerDown={onPointerDown}
        title={listening ? 'drag to move · tap to stop' : 'drag to move · tap to talk'}
        style={{
          position: 'relative',
          width: ORB,
          height: ORB,
          pointerEvents: 'auto',
          cursor: grabbing ? 'grabbing' : 'grab',
          touchAction: 'none',
        }}
      >
        <div style={{ position: 'absolute', inset: 0 }}>
          <VoiceOrb
            listening={listening}
            speaking={speaking}
            getMicAnalyser={getMicAnalyser}
            size={ORB}
          />
        </div>
        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <div
            style={{
              width: 70,
              height: 70,
              borderRadius: '50%',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              background: listening
                ? 'radial-gradient(circle at 50% 34%, rgba(120,160,255,0.30), rgba(10,14,22,0.5))'
                : 'radial-gradient(circle at 50% 34%, rgba(120,160,255,0.14), rgba(10,14,22,0.46))',
              border: `1.5px solid rgba(120,160,255,${listening ? 0.65 : 0.32})`,
              backdropFilter: 'blur(4px)',
              WebkitBackdropFilter: 'blur(4px)',
              color: BLUE,
              transition: 'border-color 0.3s, background 0.3s',
              boxShadow: listening
                ? '0 0 24px rgba(120,160,255,0.4)'
                : '0 0 14px rgba(120,160,255,0.16)',
            }}
          >
            <svg
              width="34"
              height="34"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <rect x="9" y="2" width="6" height="11" rx="3" />
              <path d="M5 10v1a7 7 0 0 0 14 0v-1" />
              <line x1="12" y1="18" x2="12" y2="22" />
              <line x1="8" y1="22" x2="16" y2="22" />
            </svg>
          </div>
        </div>
      </div>
      {/* status pill + stop — travel with the mic */}
      <div
        style={{
          marginTop: -4,
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          pointerEvents: 'auto',
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            padding: '5px 12px',
            borderRadius: 999,
            background: 'var(--glass-bg)',
            border: '1px solid var(--glass-border)',
            backdropFilter: 'blur(var(--glass-blur))',
            WebkitBackdropFilter: 'blur(var(--glass-blur))',
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            whiteSpace: 'nowrap',
            color: listening || speaking ? BLUE : 'var(--text-muted)',
          }}
        >
          {speaking && <SpeakingBars />}
          <span>{status}</span>
        </div>
        {busy && (
          <button
            onClick={onStop}
            className="tap-pop"
            style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
              cursor: 'pointer',
              padding: '5px 12px',
              borderRadius: 999,
              background: 'rgba(255,51,102,0.1)',
              border: '1px solid rgba(255,51,102,0.3)',
              color: 'var(--neon-red)',
            }}
          >
            stop
          </button>
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
        <span
          key={i}
          style={{
            width: 3,
            height: 12,
            background: BLUE,
            borderRadius: 2,
            transformOrigin: 'bottom',
            animation: `voice-bar 0.9s ease-in-out ${i * 0.12}s infinite`,
          }}
        />
      ))}
    </div>
  )
}
