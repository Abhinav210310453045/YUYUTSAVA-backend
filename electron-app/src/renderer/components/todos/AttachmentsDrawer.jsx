import React, { useState, useRef, useCallback } from 'react'
import { deleteTodoAttachment } from '../../api/client'
import { humanAge } from './shared'
import { resolveBlock } from './artifactBlocks'
import ResizeHandle from '../common/ResizeHandle'

// The card's attachment gallery as a bottom drawer — a slim always-visible
// bar that rises to a resizable panel (VS Code terminal style). Tiles carry
// an "on: <objective>" tag when the upload was dropped onto an objective
// (a meta snapshot, so it survives objective deletion).

const mono = { fontFamily: 'var(--font-mono)' }

const ATT_H_KEY = 'yy.todo.attH'
const ATT_MIN = 180

const btnStyle = (color, borderColor) => ({
  ...mono, fontSize: 10, padding: '3px 9px',
  background: 'transparent', color,
  border: `1px solid ${borderColor}`, borderRadius: 6, cursor: 'pointer',
})

// Per-kind badge tint — image/diagram lean blue (visual), link violet.
const KIND_COLOR = {
  image: { fg: 'var(--text-info)', bg: 'rgba(120,160,255,0.12)', border: 'rgba(120,160,255,0.25)' },
  diagram: { fg: 'var(--text-info)', bg: 'rgba(120,160,255,0.12)', border: 'rgba(120,160,255,0.25)' },
  link: { fg: 'var(--text-lavender)', bg: 'rgba(167,139,250,0.12)', border: 'rgba(167,139,250,0.25)' },
}
const KIND_NEUTRAL = { fg: 'var(--text-muted)', bg: 'transparent', border: 'var(--border-card)' }

// Kinds whose preview is a static render — clicking it opens the big view.
const CLICK_TO_EXPAND = new Set(['image', 'diagram'])

function AttachmentTile({ attachment, cardId, onDeleted, onExpand }) {
  const [busy, setBusy] = useState(false)
  const Block = resolveBlock(attachment)
  const kind = KIND_COLOR[attachment.kind] || KIND_NEUTRAL
  const clickable = CLICK_TO_EXPAND.has(attachment.kind)
  const onObjective = attachment.meta?.objective_title

  const onDelete = async () => {
    if (!confirm('Delete this attachment?')) return
    setBusy(true)
    try {
      await deleteTodoAttachment(cardId, attachment.attachment_id)
      onDeleted?.(attachment.attachment_id)
    } catch (e) {
      alert(`Delete failed: ${e.message}`)
      setBusy(false)
    }
  }

  return (
    <div style={{
      background: 'var(--bg-elevated, #1a1a2e)',
      border: '1px solid var(--border-card)',
      borderRadius: 8,
      padding: '10px 12px',
      display: 'flex', flexDirection: 'column', gap: 8,
      ...mono, fontSize: 12,
      minWidth: 0,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{
          fontSize: 9, padding: '1px 6px', borderRadius: 8,
          background: kind.bg, color: kind.fg, border: `1px solid ${kind.border}`,
          textTransform: 'uppercase', letterSpacing: '0.05em',
        }}>
          {attachment.kind}
        </span>
        {onObjective && (
          <span
            title={`attached on objective “${onObjective}”`}
            style={{
              fontSize: 9, padding: '1px 6px', borderRadius: 8,
              background: 'rgba(167,139,250,0.10)', color: 'var(--text-lavender)',
              border: '1px solid rgba(167,139,250,0.25)',
              maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}
          >
            on: {onObjective}
          </span>
        )}
        <span style={{
          flex: 1, minWidth: 0, color: 'var(--text-muted)', fontSize: 10,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {attachment.title || ''}
        </span>
        <span style={{ color: 'var(--text-dim)', fontSize: 10, whiteSpace: 'nowrap' }}>
          {humanAge(attachment.created_ts)}
        </span>
        <button
          onClick={() => onExpand?.(attachment)}
          title="open big view"
          style={btnStyle('var(--text-info)', 'rgba(120,160,255,0.3)')}
        >
          ⤢
        </button>
        <button
          onClick={onDelete}
          disabled={busy}
          style={btnStyle('var(--neon-red)', 'rgba(255,51,102,0.25)')}
        >
          {busy ? '...' : 'Delete'}
        </button>
      </div>
      <div
        onClick={clickable ? () => onExpand?.(attachment) : undefined}
        style={{ minWidth: 0, cursor: clickable ? 'zoom-in' : 'default' }}
      >
        <Block attachment={attachment} cardId={cardId} />
      </div>
    </div>
  )
}

export default function AttachmentsDrawer({
  cardId, attachments, open, onToggle,
  uploading = 0, onFiles, onDeleted, onExpand,
}) {
  // Drawer height (open state) — default ~40% of the window, persisted.
  const [height, setHeight] = useState(() => {
    const v = Number(localStorage.getItem(ATT_H_KEY))
    const max = Math.round(window.innerHeight * 0.6)
    return v >= ATT_MIN && v <= max ? v : Math.round(window.innerHeight * 0.4)
  })
  const [hDragging, setHDragging] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef(null)

  const startHDrag = useCallback((e) => {
    e.preventDefault()
    const startY = e.clientY
    const startH = height
    setHDragging(true)
    let h = startH
    const onMove = (ev) => {
      const max = Math.round(window.innerHeight * 0.6)
      h = Math.min(max, Math.max(ATT_MIN, startH - (ev.clientY - startY)))
      setHeight(h)
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      setHDragging(false)
      localStorage.setItem(ATT_H_KEY, String(h))
    }
    document.body.style.cursor = 'row-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }, [height])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flexShrink: 0, borderTop: '1px solid var(--border-subtle)' }}>
      {open && <ResizeHandle onMouseDown={startHDrag} side="top" />}

      {/* The always-visible bar — the drawer's toggle and drop hint. */}
      <div
        onClick={() => onToggle(!open)}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragOver(false)
          if (e.dataTransfer?.files?.length) onFiles(e.dataTransfer.files, {})
        }}
        style={{
          height: 32, flexShrink: 0,
          display: 'flex', alignItems: 'center', gap: 10, padding: '0 16px',
          cursor: 'pointer', userSelect: 'none',
          background: dragOver ? 'rgba(var(--accent-rgb),0.06)' : 'var(--bg-card)',
          borderBottom: open ? '1px solid var(--border-subtle)' : 'none',
        }}
      >
        <span style={{ ...mono, fontSize: 11, color: 'var(--text-muted)' }}>
          {open ? '▾' : '▸'} Attachments
        </span>
        {attachments.length > 0 && (
          <span style={{
            ...mono, fontSize: 10, color: 'var(--neon-green)',
            background: 'rgba(var(--accent-rgb),0.08)', border: '1px solid rgba(var(--accent-rgb),0.2)',
            borderRadius: 10, padding: '0 7px',
          }}>
            {attachments.length}
          </span>
        )}
        {uploading > 0 && (
          <span style={{ ...mono, fontSize: 10, color: 'var(--neon-amber)' }}>uploading…</span>
        )}
        <span style={{ flex: 1 }} />
        <span style={{ ...mono, fontSize: 9, color: 'var(--text-dim)' }}>
          drop files anywhere on the board — onto an objective to tag it
        </span>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          style={{ display: 'none' }}
          onChange={(e) => { onFiles(e.target.files, {}); e.target.value = '' }}
        />
        <button
          onClick={(e) => { e.stopPropagation(); fileInputRef.current?.click() }}
          disabled={uploading > 0}
          style={{
            ...mono, fontSize: 10, padding: '3px 10px',
            background: 'rgba(var(--accent-rgb),0.08)', color: 'var(--neon-green)',
            border: '1px solid rgba(var(--accent-rgb),0.25)', borderRadius: 6,
            cursor: uploading > 0 ? 'default' : 'pointer',
            opacity: uploading > 0 ? 0.5 : 1,
          }}
        >
          + Upload
        </button>
      </div>

      {/* The gallery panel. */}
      {open && (
        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragOver(false)
            if (e.dataTransfer?.files?.length) onFiles(e.dataTransfer.files, {})
          }}
          style={{
            height, overflowY: 'auto', padding: 14,
            background: dragOver ? 'rgba(var(--accent-rgb),0.03)' : 'transparent',
            transition: hDragging ? 'none' : 'height 0.15s ease',
          }}
        >
          {attachments.length === 0 ? (
            <div style={{ ...mono, color: 'var(--text-muted)', fontSize: 12 }}>
              {'> drop files here, upload, or ask the TinkerAgent for a diagram'}
            </div>
          ) : (
            <div style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))',
              gap: 12,
            }}>
              {attachments.map((a) => (
                <AttachmentTile
                  key={a.attachment_id}
                  attachment={a}
                  cardId={cardId}
                  onDeleted={onDeleted}
                  onExpand={onExpand}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
