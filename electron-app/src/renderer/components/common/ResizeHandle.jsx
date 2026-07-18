import React, { useState } from 'react'

// Drag bar between two split panes (activity rail, card view's tinker chat,
// attachments drawer). side="left"/"right" → vertical bar between columns;
// side="top"/"bottom" → horizontal bar between rows. The owner wires the
// mousemove logic via onMouseDown.
export default function ResizeHandle({ onMouseDown, side }) {
  const [hovered, setHovered] = useState(false)
  const horizontal = side === 'top' || side === 'bottom'
  return (
    <div
      onMouseDown={onMouseDown}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        ...(horizontal ? { height: 4 } : { width: 4 }),
        flexShrink: 0,
        cursor: horizontal ? 'row-resize' : 'col-resize',
        background: hovered ? 'var(--neon-green)' : 'transparent',
        opacity: hovered ? 0.4 : 1,
        transition: 'background 0.15s',
        zIndex: 10,
        position: 'relative',
      }}
    >
      {/* wider invisible hit area */}
      <div style={horizontal ? {
        position: 'absolute',
        left: 0, right: 0,
        top: -4, bottom: -4,
      } : {
        position: 'absolute',
        top: 0, bottom: 0,
        left: -4, right: -4,
      }} />
    </div>
  )
}
