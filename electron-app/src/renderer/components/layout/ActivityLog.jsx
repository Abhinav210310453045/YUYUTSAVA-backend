import React, { useEffect, useRef, useState } from 'react'

const KIND_STYLE = {
  log:         { color: '#5eead4' },
  token:       { color: 'var(--neon-green)' },
  tool_call:   { color: 'var(--neon-amber)', prefix: '→ ' },
  tool_result: { color: 'var(--text-secondary)', prefix: '← ' },
  timeline:    { color: 'var(--text-primary)', borderLeft: '2px solid var(--neon-purple)', paddingLeft: 6 },
  default:     { color: 'var(--text-muted)' },
}

function fmtTime(ts) {
  const d = new Date(ts * 1000)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

export default function ActivityLog({ lines, width }) {
  const bottomRef = useRef(null)
  const containerRef = useRef(null)
  const [autoScroll, setAutoScroll] = useState(true)

  useEffect(() => {
    if (autoScroll && bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [lines, autoScroll])

  const handleScroll = () => {
    const el = containerRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    setAutoScroll(atBottom)
  }

  return (
    <div style={{
      width: width ?? 'var(--activity-w)',
      borderLeft: '1px solid var(--border-subtle)',
      background: 'var(--bg-panel)',
      display: 'flex',
      flexDirection: 'column',
      flexShrink: 0,
    }}>
      <div style={{
        padding: '8px 12px',
        borderBottom: '1px solid var(--border-subtle)',
        fontFamily: 'var(--font-mono)',
        fontSize: 10,
        color: 'var(--text-secondary)',
        letterSpacing: '0.1em',
        textTransform: 'uppercase',
        flexShrink: 0,
      }}>
        Activity
      </div>

      <div
        ref={containerRef}
        onScroll={handleScroll}
        style={{
          flex: 1,
          overflowY: 'auto',
          padding: '8px 0',
          position: 'relative',
        }}
      >
        {/* Subtle scan-line overlay */}
        <div style={{
          position: 'absolute',
          inset: 0,
          pointerEvents: 'none',
          backgroundImage: 'repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px)',
          zIndex: 0,
        }} />

        {lines.length === 0 && (
          <div style={{ padding: '20px 12px', color: 'var(--text-dim)', fontFamily: 'var(--font-mono)', fontSize: 11 }}>
            {'> awaiting events...'}
            <span style={{ animation: 'blink 1s step-end infinite' }}>_</span>
          </div>
        )}

        {lines.map((line, i) => {
          const style = KIND_STYLE[line.kind] || KIND_STYLE.default
          const prefix = style.prefix || ''
          return (
            <div key={i} style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 10,
              lineHeight: 1.5,
              padding: '1px 12px',
              display: 'flex',
              gap: 8,
              ...style,
              animation: i === lines.length - 1 ? 'fade-in 0.2s ease' : undefined,
              position: 'relative',
              zIndex: 1,
            }}>
              <span style={{ color: 'var(--text-dim)', flexShrink: 0, fontSize: 9 }}>
                {fmtTime(line.ts || Date.now() / 1000)}
              </span>
              <span className="selectable" style={{ wordBreak: 'break-all' }}>
                {prefix}{line.text}
              </span>
            </div>
          )
        })}
        <div ref={bottomRef} />
      </div>

      {!autoScroll && (
        <button
          onClick={() => { setAutoScroll(true); bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }}
          style={{
            position: 'absolute',
            bottom: 12,
            right: 16,
            fontSize: 10,
            fontFamily: 'var(--font-mono)',
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border-neon)',
            color: 'var(--neon-green)',
            borderRadius: 4,
            padding: '3px 8px',
            cursor: 'pointer',
          }}
        >
          ↓ latest
        </button>
      )}
    </div>
  )
}
