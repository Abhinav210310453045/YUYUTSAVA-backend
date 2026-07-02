import React, { useState } from 'react'

// Small "＋ New" button used in the Chat and Voice headers to start a fresh
// conversation in-place (the panel stays mounted; only the thread resets).
export default function NewSessionButton({ onClick, label = 'New', color = 'var(--neon-green)' }) {
  const [hover, setHover] = useState(false)
  return (
    <button
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      title="Start a new conversation"
      style={{
        marginLeft: 'auto',
        display: 'inline-flex', alignItems: 'center', gap: 5,
        fontFamily: 'var(--font-mono)', fontSize: 10, letterSpacing: '0.08em',
        textTransform: 'uppercase', fontWeight: 600,
        color: hover ? '#04120b' : color,
        background: hover ? color : 'transparent',
        border: `1px solid ${color}`,
        borderRadius: 6, padding: '4px 10px', cursor: 'pointer',
        transition: 'transform 0.15s ease, background 0.15s ease, color 0.15s ease, box-shadow 0.15s ease',
        transform: hover ? 'translateY(-1px)' : 'none',
        boxShadow: hover ? `0 0 12px ${color}66` : 'none',
      }}
    >
      <span style={{ fontSize: 12, lineHeight: 1 }}>＋</span>
      <span>{label}</span>
    </button>
  )
}
