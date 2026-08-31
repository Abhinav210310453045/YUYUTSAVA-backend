import React, { useState } from 'react'
import { useNav } from '../../nav/NavProvider'

// Chevron in the same envelope as the nav glyphs (navIcons.jsx): 24-box,
// currentColor stroke, round caps.
export const BackChevron = ({ size = 16 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="15 18 9 12 15 6"/>
  </svg>
)

// One back control, two dresses. `variant="icon"` is the 28×28 titlebar button
// (same recipe as the nav icons); `variant="labelled"` is the bordered pill
// panel headers use, where a word reads better than a bare glyph.
//
// Both pop the ACTIVE TAB's stack — back never jumps between tabs, so it's
// disabled at a tab's home view rather than teleporting you somewhere else.
export default function BackButton({ variant = 'icon', label = 'Back', title, style }) {
  const { canGoBack, pop } = useNav()
  const [hover, setHover] = useState(false)
  const enabled = canGoBack
  const tip = title || (enabled ? `${label} (⌘[)` : label)

  const common = {
    onClick: enabled ? pop : undefined,
    onMouseEnter: () => setHover(true),
    onMouseLeave: () => setHover(false),
    disabled: !enabled,
    title: tip,
    'aria-label': tip,
  }

  if (variant === 'labelled') {
    return (
      <button
        {...common}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          padding: '5px 12px',
          background: hover && enabled ? 'rgba(var(--accent-rgb),0.08)' : 'transparent',
          color: enabled ? (hover ? 'var(--neon-green)' : 'var(--text-muted)') : 'var(--text-dim)',
          border: `1px solid ${hover && enabled ? 'rgba(var(--accent-rgb),0.25)' : 'var(--border-card)'}`,
          borderRadius: 6,
          cursor: enabled ? 'pointer' : 'default',
          opacity: enabled ? 1 : 0.4,
          transition: 'all 0.2s',
          ...style,
        }}
      >
        <BackChevron size={13} />
        {label}
      </button>
    )
  }

  return (
    <button
      {...common}
      style={{
        width: 28,
        height: 28,
        borderRadius: 6,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: enabled ? (hover ? 'var(--neon-green)' : 'var(--text-muted)') : 'var(--text-dim)',
        background: hover && enabled ? 'rgba(var(--accent-rgb),0.08)' : 'transparent',
        border: `1px solid ${hover && enabled ? 'rgba(var(--accent-rgb),0.2)' : 'transparent'}`,
        cursor: enabled ? 'pointer' : 'default',
        opacity: enabled ? 1 : 0.25,
        transition: 'all 0.2s',
        flexShrink: 0,
        ...style,
      }}
    >
      <BackChevron />
    </button>
  )
}
