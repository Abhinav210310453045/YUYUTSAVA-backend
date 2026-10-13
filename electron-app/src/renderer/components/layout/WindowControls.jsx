import React, { useEffect, useState } from 'react'

// Minimise, maximise and close — for a window that has no titlebar to put them in.
//
// The window is created with `frame: false` (src/main/index.js), which removes
// the OS titlebar everywhere. On macOS `titleBarStyle: 'hidden'` leaves the
// native traffic lights behind, so macOS was fine and nobody noticed that on
// Windows and Linux there was nothing at all: no close, no minimise, no
// maximise. The IPC and the preload calls had been there the whole time with
// nothing in the renderer ever calling them.
//
// Two things decide whether this works at all:
//   • the container must be `no-drag`, or the titlebar's drag region swallows
//     every click before a button sees it;
//   • it must not render on macOS, where the OS already draws its own set.

const SIZE = 28

function Minimise() {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden>
      <rect x="1.5" y="5.5" width="9" height="1" fill="currentColor" />
    </svg>
  )
}

function Maximise() {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" fill="none" aria-hidden>
      <rect x="2" y="2" width="8" height="8" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  )
}

// Two offset frames, the way every platform draws "put it back".
function Restore() {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" fill="none" aria-hidden>
      <rect x="1.6" y="3.6" width="6.8" height="6.8" stroke="currentColor" strokeWidth="1.2" />
      <path d="M3.9 3.4V1.6h6.5v6.5H8.6" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  )
}

function Close() {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden>
      <path
        d="M2.4 2.4l7.2 7.2M9.6 2.4l-7.2 7.2"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
      />
    </svg>
  )
}

function Control({ name, label, onClick, danger, children }) {
  const [hover, setHover] = useState(false)
  return (
    <button
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      title={label}
      aria-label={label}
      data-window-control={name}
      style={{
        width: SIZE,
        height: SIZE,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        border: 'none',
        borderRadius: 'var(--radius-card)',
        background: hover ? (danger ? '#e81123' : 'var(--bg-elevated)') : 'transparent',
        color: hover && danger ? '#fff' : 'var(--text-secondary)',
        cursor: 'pointer',
        transition: 'background 0.12s, color 0.12s',
      }}
    >
      {children}
    </button>
  )
}

export default function WindowControls() {
  const api = typeof window !== 'undefined' ? window.electronAPI : undefined
  const [maximized, setMaximized] = useState(false)

  useEffect(() => {
    if (!api?.onWindowMaximized) return undefined
    // Ask once for where it is now — a renderer that reloaded while the window
    // was maximised would otherwise start showing the wrong icon — then follow
    // every change, whoever caused it.
    api
      .isWindowMaximized?.()
      .then(setMaximized)
      .catch(() => {})
    return api.onWindowMaximized(setMaximized)
  }, [api])

  // Nothing to draw outside Electron (browser dev), and nothing to draw on
  // macOS, where the traffic lights are the OS's own.
  if (!api?.minimizeWindow || api.platform === 'darwin') return null

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 2,
        marginLeft: 6,
        WebkitAppRegion: 'no-drag',
      }}
    >
      <Control name="minimise" label="Minimise" onClick={() => api.minimizeWindow()}>
        <Minimise />
      </Control>
      <Control
        name="maximise"
        label={maximized ? 'Restore' : 'Maximise'}
        onClick={() => api.maximizeWindow()}
      >
        {maximized ? <Restore /> : <Maximise />}
      </Control>
      <Control name="close" label="Close" onClick={() => api.closeWindow()} danger>
        <Close />
      </Control>
    </div>
  )
}
