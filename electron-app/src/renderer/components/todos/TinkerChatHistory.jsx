import React, { useState } from 'react'
import { humanAge } from './shared'

// Clock button + dropdown listing a card's past tinker chats (newest first).
// Rows are named Claude-Code style: the chat's first user message (server-set
// session title) with a relative time + message count underneath. Selecting a
// row hands the session back to the host, which repoints the ChatPanel at it.
export default function TinkerChatHistory({ chats, activeId, onSelect, onRefresh }) {
  const [open, setOpen] = useState(false)
  const [hover, setHover] = useState(false)
  const [hoverId, setHoverId] = useState(null)

  const toggle = () => {
    const next = !open
    setOpen(next)
    if (next) onRefresh?.() // list can be stale (titles land after first turn)
  }

  // Zero-turn sessions are pending server discard — never worth listing,
  // except the one the pane is live on right now.
  const rows = (chats || []).filter((s) => s.message_count > 0 || s.id === activeId)

  const color = 'var(--text-info)'
  return (
    <span style={{ position: 'relative', display: 'inline-flex' }}>
      <button
        onClick={toggle}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        title="Chat history — continue an earlier tinker chat"
        style={{
          display: 'inline-flex', alignItems: 'center', gap: 5,
          fontFamily: 'var(--font-mono)', fontSize: 10, letterSpacing: '0.08em',
          textTransform: 'uppercase', fontWeight: 'var(--fw-semibold)',
          color,
          background: hover || open ? 'rgba(120,160,255,0.14)' : 'transparent',
          border: '1px solid rgba(120,160,255,0.3)',
          borderRadius: 6, padding: '4px 10px', cursor: 'pointer',
          transition: 'background 0.15s ease, transform 0.15s ease',
          transform: hover ? 'translateY(-1px)' : 'none',
        }}
      >
        <span style={{ fontSize: 12, lineHeight: 1 }}>⏱</span>
        {rows.length > 0 && <span>{rows.length}</span>}
      </button>

      {open && (
        <>
          {/* click-away backdrop */}
          <div
            style={{ position: 'fixed', inset: 0, zIndex: 19 }}
            onClick={() => setOpen(false)}
          />
          <div style={{
            position: 'absolute', top: 'calc(100% + 6px)', right: 0, zIndex: 20,
            width: 280, maxHeight: 320, overflowY: 'auto',
            background: 'var(--bg-elevated, #1a1a2e)',
            border: '1px solid var(--border-card)', borderRadius: 8,
            boxShadow: '0 8px 30px rgba(0,0,0,0.5)',
            padding: 4, display: 'flex', flexDirection: 'column', gap: 2,
          }}>
            {rows.length === 0 && (
              <div style={{
                fontFamily: 'var(--font-mono)', fontSize: 10,
                color: 'var(--text-dim)', padding: '10px 12px',
              }}>
                no past chats on this card yet
              </div>
            )}
            {rows.map((s) => {
              const isActive = s.id === activeId
              const isHover = hoverId === s.id
              return (
                <button
                  key={s.id}
                  onClick={() => { setOpen(false); onSelect?.(s) }}
                  onMouseEnter={() => setHoverId(s.id)}
                  onMouseLeave={() => setHoverId(null)}
                  style={{
                    display: 'flex', flexDirection: 'column', gap: 3,
                    textAlign: 'left', width: '100%', cursor: 'pointer',
                    background: isActive
                      ? 'rgba(var(--accent-rgb),0.10)'
                      : isHover ? 'var(--bg-hover, rgba(255,255,255,0.04))' : 'transparent',
                    border: 'none', borderRadius: 6, padding: '7px 10px',
                    borderLeft: isActive
                      ? '2px solid rgba(var(--accent-rgb),0.8)'
                      : '2px solid transparent',
                  }}
                >
                  <span style={{
                    fontFamily: 'var(--font-mono)', fontSize: 11,
                    color: 'var(--text-primary)',
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    maxWidth: '100%',
                  }}>
                    {s.title || s.task_preview || s.id.slice(0, 12)}
                  </span>
                  <span style={{
                    fontFamily: 'var(--font-mono)', fontSize: 9,
                    color: 'var(--text-dim)',
                  }}>
                    {humanAge(s.updated_at)} · {s.message_count} msgs{isActive ? ' · current' : ''}
                  </span>
                </button>
              )
            })}
          </div>
        </>
      )}
    </span>
  )
}
