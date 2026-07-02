import React, { useState } from 'react'
import { visualUrl, deleteVisual } from '../../api/client'

// Copy / Download / Delete toolbar shared by every artifact surface (the
// Artifacts gallery cards, the inline chat/voice images, and the full-screen
// Lightbox). Kept compact and glassy so it reads as an accent, not clutter.
//
//   • Copy     — puts the image on the clipboard (PNG); falls back to the URL.
//   • Download — native "save as" dialog, then writes the bytes where you pick.
//   • Delete   — two-tap confirm, then erases it everywhere the agent saved it
//                (row + on-disk image). Your downloaded copy is untouched.
//
// props: { visual, onDeleted, dark }
//   visual   = { visual_id, url, kind, title, mime }
//   onDeleted(visual_id) — parent removes it from its list / closes the zoom.

const EXT_BY_MIME = { 'image/png': 'png', 'image/svg+xml': 'svg', 'image/jpeg': 'jpg' }

function slug(s) {
  return (s || 'visual').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48) || 'visual'
}

function IconCopy() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" />
    </svg>
  )
}
function IconDownload() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3v12" /><path d="M7 10l5 5 5-5" /><path d="M4 20h16" />
    </svg>
  )
}
function IconTrash() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 7h16" /><path d="M9 7V4h6v3" /><path d="M6 7l1 13h10l1-13" />
    </svg>
  )
}

export default function VisualActions({ visual, onDeleted, dark = false }) {
  const [copied, setCopied] = useState(false)
  const [saved, setSaved] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)

  const stop = (e) => { e.stopPropagation(); e.preventDefault() }

  const onCopy = async (e) => {
    stop(e)
    try {
      const blob = await (await fetch(visualUrl(visual.url))).blob()
      // Chromium's clipboard only reliably writes PNG; for anything else (SVG)
      // fall through to copying the URL as text.
      if (blob.type === 'image/png' && window.ClipboardItem) {
        await navigator.clipboard.write([new window.ClipboardItem({ [blob.type]: blob })])
      } else {
        await navigator.clipboard.writeText(visualUrl(visual.url))
      }
      setCopied(true); setTimeout(() => setCopied(false), 1400)
    } catch {
      try { await navigator.clipboard.writeText(visualUrl(visual.url)); setCopied(true); setTimeout(() => setCopied(false), 1400) } catch { /* ignore */ }
    }
  }

  const onDownload = async (e) => {
    stop(e)
    try {
      const blob = await (await fetch(visualUrl(visual.url))).blob()
      const ext = EXT_BY_MIME[visual.mime || blob.type] || 'png'
      const name = `${slug(visual.title || visual.kind)}.${ext}`
      if (window.electronAPI?.saveFile) {
        const data = new Uint8Array(await blob.arrayBuffer())
        const path = await window.electronAPI.saveFile(name, data)
        if (path) { setSaved(true); setTimeout(() => setSaved(false), 1600) }
      } else {
        // Browser dev fallback: trigger a normal download.
        const a = document.createElement('a')
        a.href = URL.createObjectURL(blob); a.download = name; a.click()
        URL.revokeObjectURL(a.href)
        setSaved(true); setTimeout(() => setSaved(false), 1600)
      }
    } catch { /* ignore */ }
  }

  const onDelete = async (e) => {
    stop(e)
    if (!confirming) { setConfirming(true); setTimeout(() => setConfirming(false), 2600); return }
    setBusy(true)
    try {
      await deleteVisual(visual.visual_id)
      onDeleted?.(visual.visual_id)
    } catch {
      setBusy(false); setConfirming(false)
    }
  }

  const base = {
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 5,
    height: 26, padding: '0 9px', borderRadius: 8, cursor: 'pointer',
    fontFamily: 'var(--font-mono)', fontSize: 10, lineHeight: 1,
    background: dark ? 'rgba(255,255,255,0.10)' : 'var(--glass-bg)',
    border: '1px solid var(--glass-border)', color: 'var(--text-secondary)',
    backdropFilter: 'blur(8px)', WebkitBackdropFilter: 'blur(8px)',
    transition: 'background 0.15s, color 0.15s, border-color 0.15s',
  }
  const accent = (c) => ({ color: c, borderColor: `${c}66` })

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }} onClick={stop}>
      <button className="tap-pop" title="Copy image" onClick={onCopy}
        style={{ ...base, ...(copied ? accent('var(--neon-green)') : {}) }}>
        {copied ? '✓' : <IconCopy />}
      </button>
      <button className="tap-pop" title="Download (choose where to save)" onClick={onDownload}
        style={{ ...base, ...(saved ? accent('var(--neon-cyan)') : {}) }}>
        {saved ? '✓ saved' : <IconDownload />}
      </button>
      <button className="tap-pop" title="Delete everywhere the agent saved it" onClick={onDelete}
        disabled={busy}
        style={{ ...base, ...(confirming ? accent('var(--neon-red)') : {}), opacity: busy ? 0.5 : 1 }}>
        {confirming ? 'delete?' : <IconTrash />}
      </button>
    </div>
  )
}
